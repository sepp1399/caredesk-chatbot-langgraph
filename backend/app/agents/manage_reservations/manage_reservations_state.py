"""Manage-reservations FSM state.

Funnel: list → select → action (cancel or reschedule) → confirm. The
reschedule branch reuses lab_booking's slot search (search_dates /
get_new_dates) via the same SLOT_CACHE.
"""

import json
from datetime import datetime
from typing import Annotated, Sequence

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.managed import RemainingSteps
from typing_extensions import NotRequired, TypedDict


PHASE_NAMES: dict[int, str] = {
    1: "LIST",
    2: "SELECT_RESERVATION",
    3: "PICK_ACTION",
    4: "PICK_NEW_SLOT",  # reschedule path only
    5: "DONE",
}

# Phase gates are guardrails on TOOL CALLS only. They never script the
# message text — the system prompt's "Dialogo flessibile" section governs
# HOW to converse.
_PHASE_GATE: dict[int, str] = {
    1: "Reservations list not yet loaded. "
       "Tool guardrail: call list_my_reservations silently as the first step; do not call cancel/reschedule until a reservation is picked.",
    2: "Reservations loaded, none picked yet. "
       "Tool guardrail: do not call action tools (cancel/reschedule, search_dates) until the patient selects one in the current turn. Track the chosen resid internally.",
    3: "Reservation picked, action not chosen yet. "
       "Tool guardrail: only call cancel_reservation after explicit patient confirmation on a summary you presented; for reschedule, move to phase 4 first.",
    4: "Reschedule path — new slot not yet confirmed. "
       "Tool guardrail: re-use search_dates / get_new_dates with the original activityid (and insurance/area/doctor when known). Call reschedule_reservation only after the patient explicitly confirms the new slot.",
    5: "Operation complete. No more tool calls; close the conversation warmly.",
}


class AgentState(TypedDict):
    messages:        Annotated[Sequence[BaseMessage], lambda x, y: list(x) + list(y)]
    remaining_steps: NotRequired[RemainingSteps]
    manage:          NotRequired[dict]
    user:            NotRequired[dict]


def current_phase(m: dict) -> int:
    if m.get("done"):
        return 5
    if m.get("action") == "reschedule" and m.get("picked_resid") and not m.get("new_slot"):
        return 4
    if m.get("picked_resid"):
        return 3
    if m.get("listed"):
        return 2
    return 1


def update_manage(manage: dict, msg: ToolMessage) -> None:
    try:
        data = json.loads(msg.content)
    except Exception:
        return
    if not isinstance(data, dict) or data.get("status") != "ok":
        return

    name = getattr(msg, "name", None)
    if name == "list_my_reservations":
        manage["listed"] = True
        manage["reservations"] = data.get("reservations") or []
    elif name == "cancel_reservation":
        manage["picked_resid"] = data.get("resid")
        manage["action"] = "cancel"
        manage["done"] = True
    elif name == "reschedule_reservation":
        manage["picked_resid"] = data.get("old_resid")
        manage["action"] = "reschedule"
        manage["new_slot"] = data.get("new_booking")
        manage["done"] = True


def format_manage_snapshot(manage: dict, user: dict | None = None) -> str:
    phase = current_phase(manage)
    now   = datetime.now().strftime("%A, %d %B %Y — %H:%M")

    def tick(v: object) -> str:
        return "✓" if v else "⏳"

    auth_status = (user or {}).get("status")
    if user and user.get("userid"):
        # Past reservations are pre-loaded by the router after auth.
        past = user.get("past_reservations") or []
        past_hint = ""
        if past:
            preview = "; ".join(
                f"{r.get('Date','')} {r.get('Type','').strip()} dr.{r.get('Doctor','')}"
                for r in past[:3]
            )
            past_hint = f"\n                   · past_reservations={len(past)} (latest: {preview})"
        user_row = (
            f"{user.get('name','')} {user.get('surname','')} "
            f"(userid={user.get('userid')}, phone={user.get('phone','—')}) → AUTHENTICATED."
            + past_hint
        )
    elif auth_status == "ambiguous":
        cands = user.get("candidates", []) or []
        user_row = (
            f"AMBIGUOUS — {len(cands)} profiles for phone={user.get('phone','—')}. "
            "→ ask for date of birth and call authenticate_user_by_birthdate."
        )
    elif auth_status == "not_found":
        user_row = (
            f"NOT FOUND for phone={user.get('phone','—')}. "
            "→ ask for codice fiscale and call authenticate_user_by_codice_fiscale; "
            "if still nothing, transfer_to_flow('patient_registration', ...)."
        )
    else:
        user_row = (
            "anonymous (no caller phone) → "
            "ask for phone and call authenticate_user_by_phone or fall back to CAREDESK_TEST_USER_ID."
        )

    listed = manage.get("listed")
    reservations = manage.get("reservations") or []
    n = len(reservations)
    picked = manage.get("picked_resid")
    action = manage.get("action")
    new_slot = manage.get("new_slot")
    done = manage.get("done")

    return "\n".join([
        "━━━ MANAGE_RESERVATIONS STATE (internal — NEVER narrate) ━━━",
        f"  today          : {now}",
        f"  user           : {user_row}",
        f"  current_phase  : {phase} ({PHASE_NAMES[phase]})",
        "  ─── collected ───────────────────────────────────────────",
        f"  {tick(listed)}  listed     : {n} reservation(s) loaded",
        f"  {tick(picked)}  selected   : {picked or 'pending'}",
        f"  {tick(action)}  action     : {action or 'pending'}",
        f"  {tick(new_slot)}  new_slot   : {new_slot or 'pending'}",
        f"  {tick(done)}  done       : {bool(done)}",
        "  ─── state-only notes (no scripting) ─────────────────────",
        f"  · state          : phase {phase} = {PHASE_NAMES[phase]}",
        f"  · tool guardrail : {_PHASE_GATE[phase]}",
        "  · phrasing       : the 'Dialogo flessibile' rules from the system prompt govern HOW to talk. "
        "If the patient asks a question, answer it first; vary phrasing, never script.",
        "  · already done   : do not re-call tools for steps already marked ✓ above.",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ])
