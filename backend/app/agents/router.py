"""Top-level router — dispatches each session to a per-flow agent.

Each session is associated with one of the per-flow agents:
  lab_booking | manage_reservations | patient_registration |
  lead_creation | ivr_to_digital

The router maintains a disk-backed `session_id → flow` registry, calls the
LLM-based orchestrator on every turn to detect intent changes, and
dispatches to the active agent. Cross-flow handoff is honoured via the
`transfer_to_flow` tool — when an agent calls it, the router switches the
registry and runs the target agent on the same turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from threading import RLock
from typing import AsyncIterator, Optional

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    RemoveMessage,
)

from app.agents.lab_booking          import (
    lab_booking_agent,
    PHASE_NAMES as LAB_PHASE_NAMES,
    current_phase as lab_current_phase,
    extract_tool_trace as lab_extract_trace,
)
from app.agents.manage_reservations  import (
    manage_reservations_agent,
    extract_replies as manage_extract,
)
from app.agents.patient_registration import (
    patient_registration_agent,
    extract_replies as registration_extract,
)
from app.agents.lead_creation        import (
    lead_creation_agent,
    extract_replies as lead_extract,
)
from app.agents.ivr_to_digital       import (
    ivr_to_digital_agent,
    extract_replies as ivr_extract,
)
from app.agents.orchestrator         import aclassify_intent
from app.agents.tool_timing          import ToolTimingHandler, merge_timings
from app.observability               import (
    get_langfuse_handler,
    langfuse_metadata,
    trace_session,
)
from app.tools.shared.auth           import (
    lookup_caller_by_phone,
    load_caller_past_reservations,
)
from app.tools.shared._helpers       import normalize_digits
from app import i18n

logger = logging.getLogger("caredesk_lg.router")

_FLOW_FILE = Path(__file__).resolve().parents[2] / "logs" / "flow_registry.json"

FLOW_NAMES: tuple[str, ...] = (
    "lab_booking",
    "manage_reservations",
    "patient_registration",
    "lead_creation",
    "ivr_to_digital",
)

_AGENTS = {
    "lab_booking":          lab_booking_agent,
    "manage_reservations":  manage_reservations_agent,
    "patient_registration": patient_registration_agent,
    "lead_creation":        lead_creation_agent,
    "ivr_to_digital":       ivr_to_digital_agent,
}

_EXTRACTORS = {
    "manage_reservations":  manage_extract,
    "patient_registration": registration_extract,
    "lead_creation":        lead_extract,
    "ivr_to_digital":       ivr_extract,
}

_FLOWS_WITH_BOOKING_STATE = {"lab_booking"}
_FLOWS_WITH_MANAGE_STATE = {"manage_reservations"}


def _load_registry() -> dict[str, str]:
    if not _FLOW_FILE.exists():
        return {}
    try:
        return json.loads(_FLOW_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("registry load failed: %s — starting empty", exc)
        return {}


_registry_lock = RLock()
_registry: dict[str, str] = _load_registry()
_last_bot: dict[str, str] = {}
_caller_state: dict[str, dict] = {}


def _save_registry() -> None:
    try:
        _FLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FLOW_FILE.write_text(json.dumps(_registry, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("registry save failed: %s", exc)


def get_flow(session_id: str) -> Optional[str]:
    with _registry_lock:
        return _registry.get(session_id)


def set_flow(session_id: str, flow: str) -> None:
    with _registry_lock:
        _registry[session_id] = flow
        _save_registry()


def reset_flow(session_id: str) -> None:
    with _registry_lock:
        _registry.pop(session_id, None)
        _caller_state.pop(session_id, None)
        _save_registry()


# ── Caller pre-flight auth ──────────────────────────────────────────────────

async def ensure_caller_auth(session_id: str,
                             caller_phone: str | None) -> dict | None:
    """Look up the caller's phone once per (session_id, caller_phone)."""
    if not caller_phone:
        return None
    norm = normalize_digits(caller_phone)
    if not norm:
        return None
    cached = _caller_state.get(session_id)
    if cached and cached.get("phone") == norm:
        return cached.get("user")
    result = await lookup_caller_by_phone(norm)
    if result.get("status") == "authenticated":
        result["past_reservations"] = await load_caller_past_reservations(
            result.get("userid"),
        )
    _caller_state[session_id] = {"phone": norm, "user": result}
    logger.info("AUTH   session=%s  phone=%s  status=%s  past_res=%d",
                session_id, norm, result.get("status"),
                len(result.get("past_reservations") or []))
    return result


def get_caller_user(session_id: str) -> dict | None:
    cached = _caller_state.get(session_id)
    return cached.get("user") if cached else None


# ── Observability config ────────────────────────────────────────────────────

def _trace_config(session_id: str, flow: str, user_id: str | None = None) -> dict:
    return {
        "configurable": {"thread_id": f"{session_id}-{flow}"},
        "tags": [f"agent:{flow}", "caredesk_lg"],
        "metadata": {
            "session_id": session_id,
            "flow": flow,
            **langfuse_metadata(session_id, flow, user_id),
        },
        "run_name": f"{flow}_turn",
    }


def _kickoff_for(flow: str) -> str:
    return i18n.kickoff(flow) or "ciao"


def _agent_state_for(session_id: str, flow: str,
                     user: dict | None) -> tuple[dict, dict, ToolTimingHandler]:
    invoke_state: dict = {"messages": [HumanMessage(content="")]}
    if user is not None:
        invoke_state["user"] = user
    timing = ToolTimingHandler()
    cfg = _trace_config(session_id, flow, (user or {}).get("userid"))
    callbacks: list = [timing]
    lf = get_langfuse_handler()
    if lf is not None:
        callbacks.append(lf)
    cfg["callbacks"] = callbacks
    return invoke_state, cfg, timing


def _build_debug(flow: str, messages: list, turn: list,
                 timings: list, booking: dict | None) -> dict:
    trace = lab_extract_trace(turn)
    merge_timings(trace, timings)
    if flow == "lab_booking":
        phase = lab_current_phase(booking or {})
        return {
            "flow":       "lab_booking",
            "phase":      phase,
            "phase_name": LAB_PHASE_NAMES.get(phase, "?"),
            "booking":    {k: v for k, v in (booking or {}).items() if k != "_processed_ids"},
            "tool_trace": trace,
        }
    return {
        "flow":       flow,
        "phase":      None,
        "phase_name": flow.upper(),
        "booking":    {},
        "tool_trace": trace,
    }


def _turn_messages(messages: list) -> tuple[list, int]:
    last_human = max(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)),
        default=0,
    )
    return messages[last_human + 1:], last_human


def _replies_from_turn(turn: list) -> list[str]:
    return [
        (m.content or "").strip()
        for m in turn
        if isinstance(m, AIMessage) and (m.content or "").strip()
    ]


# ── Dispatch helpers ────────────────────────────────────────────────────────

async def _run_flow(session_id: str, flow: str, message: str) -> dict:
    """Blocking (non-streaming) run of a single agent turn."""
    user = get_caller_user(session_id)
    invoke_state, cfg, timing = _agent_state_for(session_id, flow, user)
    invoke_state["messages"] = [HumanMessage(content=message)]

    agent = _AGENTS[flow]
    user_id = (user or {}).get("userid")
    with trace_session(
        name=f"{flow}_turn",
        session_id=session_id,
        user_id=user_id,
        tags=[f"agent:{flow}", "caredesk_lg"],
        metadata={"flow": flow},
    ):
        result = await agent.ainvoke(invoke_state, config=cfg)
    messages = result["messages"]
    turn, last_human = _turn_messages(messages)

    if flow in _EXTRACTORS:
        replies = _EXTRACTORS[flow](messages, last_human)
    else:
        replies = _replies_from_turn(turn)

    booking = result.get("booking") if flow == "lab_booking" else None
    debug = _build_debug(flow, messages, turn, timing.timings, booking)
    return {
        "replies": replies,
        "reply":   replies[-1] if replies else "",
        "debug":   debug,
    }


async def _dispatch_to_flow(session_id: str, flow: str, message: str) -> dict:
    if flow in _AGENTS:
        return await _run_flow(session_id, flow, message)
    reset_flow(session_id)
    return menu_reply(prefix=i18n.fallback_routing_error())


# ── Cross-flow handoff (transfer_to_flow) ───────────────────────────────────

async def _maybe_transfer(session_id: str, source_flow: str,
                          source_result: dict) -> Optional[dict]:
    trace = source_result.get("debug", {}).get("tool_trace", []) or []
    calls = [t for t in trace if t.get("tool") == "transfer_to_flow"]
    if not calls:
        return None
    target = (calls[-1].get("input") or {}).get("target")
    if target not in _AGENTS or target == source_flow:
        return None

    set_flow(session_id, target)
    logger.info("ROUTE  session=%s  flow=%s → %s (transfer_to_flow)",
                session_id, source_flow, target)

    kickoff = _kickoff_for(target)
    target_result = await _dispatch_to_flow(session_id, target, kickoff)

    combined = (source_result.get("replies") or []) + (target_result.get("replies") or [])
    combined = [r for r in combined if r]
    target_debug = target_result.get("debug", {})
    return {
        "replies": combined,
        "reply":   combined[-1] if combined else "",
        "debug": {
            "flow":             target,
            "phase":            target_debug.get("phase"),
            "phase_name":       target_debug.get("phase_name"),
            "booking":          target_debug.get("booking", {}),
            "tool_trace":       (source_result["debug"].get("tool_trace") or [])
                              + (target_debug.get("tool_trace") or []),
            "transferred_from": source_flow,
        },
    }


# ── Intent resolution (shared by dispatch + dispatch_stream) ────────────────

async def _resolve_intent(session_id: str, text: str, caller_phone: str | None
                          ) -> tuple[Optional[str], Optional[str], dict | None, str]:
    """Returns (flow_to_run, decision, caller_user, reasoning).

    `flow_to_run` is None when the turn terminates here (menu / info / fallback)."""
    flow = get_flow(session_id)
    caller_user, (decision, reasoning) = await asyncio.gather(
        ensure_caller_auth(session_id, caller_phone),
        aclassify_intent(
            text, current_flow=flow,
            last_bot_message=_last_bot.get(session_id),
            session_id=session_id,
        ),
    )
    intent = decision if decision in _AGENTS else None
    if decision == "menu":
        return None, "menu", caller_user, reasoning
    if flow is None:
        if intent is None:
            if decision == "info":
                return None, "info", caller_user, reasoning
            return None, "fallback", caller_user, reasoning
        set_flow(session_id, intent)
        flow = intent
    elif intent and intent != flow:
        set_flow(session_id, intent)
        flow = intent
    return flow, decision, caller_user, reasoning


# ── Public entry point ──────────────────────────────────────────────────────

async def dispatch(session_id: str, message: str,
                   caller_phone: str | None = None) -> dict:
    text = message.strip()
    flow, decision, caller_user, reasoning = await _resolve_intent(
        session_id, text, caller_phone,
    )
    logger.info(
        "ROUTE  session=%s  decision=%s  flow=%s  caller=%s  reason=%.80r",
        session_id, decision, flow,
        (caller_user or {}).get("status", "anonymous"), reasoning,
    )

    if decision == "menu":
        reset_flow(session_id)
        await _clear_subagent_states(session_id)
        reply = menu_reply(prefix=None)
    elif flow is None and decision == "info":
        reply = _info_inline_reply(text)
    elif flow is None:
        reply = menu_reply(prefix=i18n.fallback_didnt_understand())
    else:
        result = await _dispatch_to_flow(session_id, flow, message)
        transferred = await _maybe_transfer(session_id, flow, result)
        reply = transferred or result

    reply.setdefault("debug", {})["caller"] = caller_user
    if reply.get("reply"):
        _last_bot[session_id] = reply["reply"]
    return reply


# ── Streaming variant ───────────────────────────────────────────────────────

def _static_reply_events(payload: dict, caller_user: dict | None):
    payload.setdefault("debug", {})["caller"] = caller_user
    for r in payload.get("replies") or []:
        yield {"type": "bubble", "content": r}
    yield {
        "type":          "final",
        "quick_replies": payload.get("quick_replies", []),
        "debug":         payload["debug"],
    }


async def dispatch_stream(session_id: str, message: str,
                          caller_phone: str | None = None) -> AsyncIterator[dict]:
    """Async generator producing SSE events for a single chat turn."""
    text = message.strip()
    flow, decision, caller_user, reasoning = await _resolve_intent(
        session_id, text, caller_phone,
    )
    logger.info(
        "STREAM session=%s  decision=%s  flow=%s  caller=%s  reason=%.80r",
        session_id, decision, flow,
        (caller_user or {}).get("status", "anonymous"), reasoning,
    )

    if decision == "menu":
        reset_flow(session_id)
        await _clear_subagent_states(session_id)
        for ev in _static_reply_events(menu_reply(prefix=None), caller_user):
            yield ev
        return

    if flow is None and decision == "info":
        for ev in _static_reply_events(_info_inline_reply(text), caller_user):
            yield ev
        return

    if flow is None:
        for ev in _static_reply_events(
            menu_reply(prefix=i18n.fallback_didnt_understand()), caller_user,
        ):
            yield ev
        return

    final_reply_parts: list[str] = []
    async for ev, result_box in _stream_flow(session_id, flow, message):
        if ev is not None:
            yield ev
            if ev.get("type") == "delta":
                final_reply_parts.append(ev["content"])
        if result_box is not None:
            transferred = await _maybe_transfer(session_id, flow, result_box)
            final = transferred or result_box
            final.setdefault("debug", {})["caller"] = caller_user
            if transferred:
                extra_replies = (transferred.get("replies") or [])[
                    len(result_box.get("replies") or []):
                ]
                for r in extra_replies:
                    yield {"type": "bubble", "content": r}
            last_reply = "".join(final_reply_parts).strip() or final.get("reply", "")
            if last_reply:
                _last_bot[session_id] = last_reply
            yield {
                "type":          "final",
                "quick_replies": final.get("quick_replies", []),
                "debug":         final["debug"],
            }


async def _stream_flow(session_id: str, flow: str, message: str
                       ) -> AsyncIterator[tuple[dict | None, dict | None]]:
    """Stream the chosen flow agent. Yields (event, result_box) tuples."""
    if flow not in _AGENTS:
        reset_flow(session_id)
        payload = menu_reply(prefix=i18n.fallback_routing_error())
        for ev in _static_reply_events(payload, None):
            yield ev, None
        yield None, payload
        return

    agent = _AGENTS[flow]
    user = get_caller_user(session_id)
    invoke_state, cfg, timing = _agent_state_for(session_id, flow, user)
    invoke_state["messages"] = [HumanMessage(content=message)]
    user_id = (user or {}).get("userid")

    saw_first_delta = False
    current_msg_id: str | None = None
    bubble_buf: list[str] = []

    with trace_session(
        name=f"{flow}_turn",
        session_id=session_id,
        user_id=user_id,
        tags=[f"agent:{flow}", "caredesk_lg"],
        metadata={"flow": flow, "stream": True},
    ):
        async for chunk, _meta in agent.astream(invoke_state, config=cfg, stream_mode="messages"):
            if not isinstance(chunk, AIMessageChunk):
                continue
            content = chunk.content or ""
            if not content:
                continue
            chunk_id = getattr(chunk, "id", None)
            if (
                current_msg_id is not None
                and chunk_id is not None
                and chunk_id != current_msg_id
            ):
                yield {"type": "delta_break"}, None
            current_msg_id = chunk_id or current_msg_id
            saw_first_delta = True
            bubble_buf.append(content)
            yield {"type": "delta", "content": content}, None

    state = agent.get_state(cfg).values
    messages = state.get("messages") or []
    turn, _ = _turn_messages(messages)
    replies = _replies_from_turn(turn)
    booking = state.get("booking") if flow == "lab_booking" else None
    debug = _build_debug(flow, messages, turn, timing.timings, booking)

    if not saw_first_delta and replies:
        for r in replies:
            yield {"type": "bubble", "content": r}, None

    yield None, {
        "replies": replies,
        "reply":   replies[-1] if replies else "".join(bubble_buf).strip(),
        "debug":   debug,
    }


# ── Menu / info helpers ─────────────────────────────────────────────────────

def menu_reply(prefix: Optional[str]) -> dict:
    welcome = i18n.welcome_lines()
    return {
        "replies": welcome if prefix is None else [prefix.strip(), *welcome],
        "reply":   welcome[-1],
        "quick_replies": i18n.welcome_quick_replies(),
        "debug": {
            "flow": None, "phase": None, "phase_name": "MENU",
            "booking": {}, "tool_trace": [],
        },
    }


_GENERIC_INFO_RE = re.compile(
    r"^\s*(?:informazion\w*|info\w*|aiut\w*|help|information|3)\s*[!.?]?\s*$", re.I,
)
_PREP_RE = re.compile(
    r"\b(?:preparazion|prepar|holter|ecg|elettrocardio|esami|preparation|exam)\w*", re.I,
)
_KB_TOP_K = 2


def _info_inline_reply(text: str) -> dict:
    text = (text or "").strip()
    debug_base = {
        "flow":       "info",
        "phase":      None,
        "booking":    {},
        "tool_trace": [],
    }

    if _GENERIC_INFO_RE.match(text):
        msg = i18n.info_menu_intro()
        return {
            "replies":       [msg],
            "reply":         msg,
            "quick_replies": i18n.info_topics(),
            "debug":         {**debug_base, "phase_name": "INFO_MENU"},
        }

    try:
        from app.tools.shared.knowledge_base import _retrieve
        chunks = []
        if _PREP_RE.search(text):
            chunks = _retrieve(text, "preparation", top_k=_KB_TOP_K)
        if not chunks:
            chunks = _retrieve(text, "faq", top_k=_KB_TOP_K)
    except Exception as exc:
        logger.warning("Inline KB failed: %s", exc)
        chunks = []

    if not chunks:
        msg = i18n.info_no_match()
    else:
        body = "\n\n".join(c.content for c in chunks)
        msg = f"{body}\n\n{i18n.info_followup()}"

    return {
        "replies":       [msg],
        "reply":         msg,
        "quick_replies": i18n.info_topics(),
        "debug":         {**debug_base, "phase_name": "INFO_INLINE"},
    }


async def _clear_subagent_states(session_id: str) -> None:
    for flow_name, agent in _AGENTS.items():
        cfg = {"configurable": {"thread_id": f"{session_id}-{flow_name}"}}
        try:
            state = agent.get_state(cfg)
            msgs = state.values.get("messages", [])
            update: dict = {}
            if msgs:
                update["messages"] = [RemoveMessage(id=m.id) for m in msgs]
            if flow_name in _FLOWS_WITH_BOOKING_STATE:
                update["booking"] = {}
            if flow_name in _FLOWS_WITH_MANAGE_STATE:
                update["manage"] = {}
            if update:
                agent.update_state(cfg, update)
        except Exception as exc:
            logger.warning("clear %s failed: %s", flow_name, exc)
