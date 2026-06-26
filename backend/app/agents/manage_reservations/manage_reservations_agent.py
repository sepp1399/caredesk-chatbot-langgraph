"""Manage-reservations agent — list, cancel and reschedule appointments.

Like lab_booking, this agent uses `create_react_agent` + a `pre_model_hook`
that injects the per-turn state snapshot. The first phase is synthesised
as an automatic `list_my_reservations` tool call so the patient never has
to wait through an explicit "loading…" step.

Reschedule reuses lab_booking's slot search tools (`search_dates`,
`get_new_dates`) and the shared SLOT_CACHE: when the patient picks a
new slot the agent calls `reschedule_reservation(resid, new_slotid)`.
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
from app.agents.manage_reservations.manage_reservations_state import (
    AgentState,
    current_phase,
    format_manage_snapshot,
    update_manage,
)
from app.tools.lab_booking.availability import get_new_dates, search_dates
from app.tools.manage_reservations import MANAGE_RESERVATIONS_TOOLS
from app.tools.manage_reservations.list_reservations import list_my_reservations
from app.tools.shared.knowledge_base import search_knowledge_base
from app.tools.shared.transfer       import transfer_to_flow
from app.i18n import apply_language, system_prompt_suffix

logger = logging.getLogger("caredesk_lg.manage_reservations")

# ── Prompt ─────────────────────────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).parent / "manage_reservations_prompt.md"


def _load_prompt() -> str:
    return apply_language(_PROMPT_PATH.read_text(encoding="utf-8")) + system_prompt_suffix()


SYSTEM_PROMPT: str = _load_prompt()


def reload_prompt() -> int:
    global SYSTEM_PROMPT
    SYSTEM_PROMPT = _load_prompt()
    return len(SYSTEM_PROMPT)


# ── Replies extractor ─────────────────────────────────────────────────────────

def extract_replies(messages: list, last_human_idx: int) -> list[str]:
    return [
        (m.content or "").strip()
        for m in messages[last_human_idx + 1:]
        if isinstance(m, AIMessage) and (m.content or "").strip()
    ]


# ── INIT synthesis: silent list_my_reservations on entry ──────────────────────

def _already_called(messages: list, name: str) -> bool:
    for m in messages:
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == name:
            return True
    return False


async def _synthesize_list_call(user: dict | None) -> list:
    """Pre-fetch the patient's reservations so the LLM never has to wait."""
    try:
        from app.tools.manage_reservations.list_reservations import (
            _list_my_reservations_impl,
        )
        userid = (user or {}).get("userid")
        result_json = await _list_my_reservations_impl(userid=userid)
    except Exception as exc:
        logger.warning("Synthetic list_my_reservations failed: %s", exc)
        return []

    tcid = str(uuid.uuid4())
    return [
        AIMessage(content="",
                  tool_calls=[{"id": tcid, "name": "list_my_reservations",
                               "args": {"userid": (user or {}).get("userid")}}]),
        ToolMessage(content=result_json,
                    tool_call_id=tcid, name="list_my_reservations"),
    ]


# ── Pre-model hook ────────────────────────────────────────────────────────────
# The state snapshot is kept SEPARATE from SYSTEM_PROMPT below so the model
# can context-cache the (stable) SYSTEM_PROMPT prefix across turns.

async def _pre_model_hook(state: AgentState) -> dict:
    manage: dict = dict(state.get("manage") or {})
    user:   dict = state.get("user") or {}
    processed_ids: set = set(manage.pop("_processed_ids", []))

    existing = list(state.get("messages") or [])

    for msg in existing:
        if not isinstance(msg, ToolMessage):
            continue
        key = msg.tool_call_id or msg.id
        if key in processed_ids:
            continue
        update_manage(manage, msg)
        processed_ids.add(key)

    extra: list = []
    if current_phase(manage) == 1 and not _already_called(existing, "list_my_reservations"):
        extra = await _synthesize_list_call(user)
        for m in extra:
            if isinstance(m, ToolMessage):
                key = m.tool_call_id or m.id
                if key not in processed_ids:
                    update_manage(manage, m)
                    processed_ids.add(key)

    manage["_processed_ids"] = list(processed_ids)
    snapshot = format_manage_snapshot(manage, user)

    out: dict = {
        "manage": manage,
        "llm_input_messages": [
            SystemMessage(content=SYSTEM_PROMPT),
            SystemMessage(content=snapshot),
            *existing,
            *extra,
        ],
    }
    if extra:
        out["messages"] = extra
    return out


# ── Compiled agent ────────────────────────────────────────────────────────────

_memory = MemorySaver()

manage_reservations_agent = create_react_agent(
    model=llm,
    tools=[
        *MANAGE_RESERVATIONS_TOOLS,
        search_dates, get_new_dates,
        search_knowledge_base, transfer_to_flow,
    ],
    state_schema=AgentState,
    pre_model_hook=_pre_model_hook,
    checkpointer=_memory,
)
logger.info(
    "ManageReservations agent ready — %d tools",
    len(MANAGE_RESERVATIONS_TOOLS) + 4,
)
