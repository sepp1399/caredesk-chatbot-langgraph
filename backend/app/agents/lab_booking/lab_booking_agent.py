"""Lab-booking agent — a five-phase booking funnel as a ReAct loop.

Each phase is gated by `pre_model_hook` which:
  (a) ingests new tool messages into the booking state,
  (b) synthesises the per-phase INIT call so the canonical list is
      always grounded,
  (c) appends the state snapshot to the system prompt so the LLM sees
      where it currently stands.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from app.agents.llms import llm
from app.agents.lab_booking.lab_booking_state import (
    AgentState,
    current_phase,
    format_booking_snapshot,
    update_booking,
)
from app.tools.lab_booking import (
    LAB_BOOKING_TOOLS,
    _fetch_activities,
    _fetch_doctor_map,
    _fetch_insurance_map,
)
from app.tools.shared.knowledge_base import search_knowledge_base
from app.tools.shared.transfer       import transfer_to_flow
from app.i18n import apply_language, system_prompt_suffix

logger = logging.getLogger("caredesk_lg.lab_booking")

# ── Prompt ─────────────────────────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).parent / "lab_booking_prompt.md"


def _load_prompt() -> str:
    """Read the canonical prompt from disk, resolve the `{LANG_NAME}`
    placeholders in the `## Lingua` section (single source of truth for
    the bot's output language), and append the functional tone /
    reactivity suffix at the end."""
    return apply_language(_PROMPT_PATH.read_text(encoding="utf-8")) + system_prompt_suffix()


SYSTEM_PROMPT: str = _load_prompt()


def reload_system_prompt() -> int:
    global SYSTEM_PROMPT
    SYSTEM_PROMPT = _load_prompt()
    return len(SYSTEM_PROMPT)


# ── Tool trace extractor (re-used by other agents via the router) ─────────────

def extract_tool_trace(messages: list) -> list[dict]:
    """Return tool calls + results for a single agent turn."""
    calls: dict[str, dict] = {}
    trace: list[dict] = []
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                entry = {"tool": tc["name"], "input": tc["args"], "output": None}
                calls[tc["id"]] = entry
                trace.append(entry)
        elif isinstance(msg, ToolMessage):
            entry = calls.get(msg.tool_call_id)
            if entry:
                try:
                    entry["output"] = json.loads(msg.content)
                except Exception:
                    entry["output"] = msg.content
    return trace


# ── Phase-entry INIT synthesis ────────────────────────────────────────────────

_PHASE_INIT_TOOL: dict[int, str] = {
    1: "search_insurance_names",
    2: "search_doctor_names",
    3: "search_available_services",
}

_FETCHERS = {
    "search_insurance_names":    _fetch_insurance_map,
    "search_doctor_names":       _fetch_doctor_map,
    "search_available_services": _fetch_activities,
}


def _already_called(messages: list, tool_name: str) -> bool:
    for m in messages:
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == tool_name:
            return True
    return False


async def _synthesize_init_call(tool_name: str) -> list:
    try:
        result = await _FETCHERS[tool_name]()
    except Exception as exc:
        logger.warning("Synthetic %s fetch failed: %s", tool_name, exc)
        return []
    tcid = str(uuid.uuid4())
    return [
        AIMessage(content="", tool_calls=[{"id": tcid, "name": tool_name, "args": {}}]),
        ToolMessage(content=json.dumps(result, ensure_ascii=False),
                    tool_call_id=tcid, name=tool_name),
    ]


# ── Pre-model hook — same conventions as the subagent's booking ──────────────
# The snapshot is kept SEPARATE from the system prompt below so the model
# can context-cache the (large, stable) SYSTEM_PROMPT prefix across turns.
# Folding the per-turn snapshot into the same SystemMessage defeats prefix
# caching — every turn would look like a fresh string to the model.

async def _pre_model_hook(state: AgentState) -> dict:
    booking: dict = dict(state.get("booking") or {})
    user:    dict = state.get("user") or {}
    processed_ids: set = set(booking.pop("_processed_ids", []))

    existing = list(state.get("messages") or [])

    for msg in existing:
        if not isinstance(msg, ToolMessage):
            continue
        key = msg.tool_call_id or msg.id
        if key in processed_ids:
            continue
        update_booking(booking, msg)
        processed_ids.add(key)

    phase = current_phase(booking)
    extra: list = []
    init_tool = _PHASE_INIT_TOOL.get(phase)
    if init_tool and not _already_called(existing, init_tool):
        extra = await _synthesize_init_call(init_tool)
        for m in extra:
            if isinstance(m, ToolMessage):
                key = m.tool_call_id or m.id
                if key not in processed_ids:
                    update_booking(booking, m)
                    processed_ids.add(key)

    booking["_processed_ids"] = list(processed_ids)
    snapshot = format_booking_snapshot(booking, user)

    out: dict = {
        "booking": booking,
        "llm_input_messages": [
            # 1st system message: large, stable prompt → cache-friendly.
            SystemMessage(content=SYSTEM_PROMPT),
            # 2nd system message: per-turn state — changes every turn, so
            # it's deliberately AFTER the cacheable prefix.
            SystemMessage(content=snapshot),
            *existing,
            *extra,
        ],
    }
    if extra:
        out["messages"] = extra
    return out


# ── Compiled agent ───────────────────────────────────────────────────────────

_memory = MemorySaver()

lab_booking_agent = create_react_agent(
    model=llm,
    tools=[*LAB_BOOKING_TOOLS, search_knowledge_base, transfer_to_flow],
    state_schema=AgentState,
    pre_model_hook=_pre_model_hook,
    checkpointer=_memory,
)
logger.info("LabBooking agent ready — %d tools", len(LAB_BOOKING_TOOLS) + 2)
