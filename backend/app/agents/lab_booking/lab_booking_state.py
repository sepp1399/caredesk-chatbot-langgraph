"""Lab-booking FSM state.

The 5 phases (insurance → doctor → service → slot → confirmation) are a
pure derivation of the booking dict. Two specifics:
- a pre-booking authentication phase (phase 0) is performed by the
  router via `authenticate_user_by_*` before lab_booking is invoked,
  so here it appears only as a precondition (`user_id` in the snapshot);
- a `deferred` flag inside the service dict signals when an activity is
  bookable only as a request and not as an immediate slot.
"""

import json
from datetime import datetime
from typing import Annotated, Sequence

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.managed import RemainingSteps
from typing_extensions import NotRequired, TypedDict

from app.config import settings


PHASE_NAMES: dict[int, str] = {
    1: "INSURANCE",
    2: "DOCTOR",
    3: "SERVICE",
    4: "DATE_TIME",
    5: "BOOKED",
}

# Phase gates are guardrails on TOOL CALLS only. They never script the
# message text — the system prompt's "Dialogo flessibile" section governs
# HOW to converse, vary phrasing, handle questions and unsure patients.
_PHASE_GATE: dict[int, str] = {
    1: "Insurance preference not yet captured. "
       "Tool guardrail: call get_insurance_id_by_insurance_name only after the patient names a plan or says 'privato' in the current turn.",
    2: "Doctor preference not yet captured. "
       "Tool guardrail: call search_doctor_names(picked_name=…) only after the patient names a doctor in the current turn (or declines preference → null).",
    3: "Service not yet picked. "
       "Tool guardrail: call search_dates only after the patient picks a service from search_available_services. "
       "If the chosen activity has mopBookability='DEFERRED_EMAIL', SKIP phase 4 and on explicit confirmation call request_deferred_appointment(activityid, ...) — no slot required.",
    4: "Slot not yet confirmed. "
       "Tool guardrail: call book_appointment only after the patient explicitly confirms a summary you presented (date, time, service, doctor; price only if asked). "
       "Time mapping when the patient gives a range: 'mattina' → 08:00-13:00, 'pomeriggio' → 13:00-21:00.",
    5: "Booking complete. No more tool calls; close the conversation warmly.",
}


class AgentState(TypedDict):
    messages:        Annotated[Sequence[BaseMessage], lambda x, y: list(x) + list(y)]
    remaining_steps: NotRequired[RemainingSteps]
    booking:         NotRequired[dict]
    user:            NotRequired[dict]   # {"userid":..., "name":..., "surname":...}


def current_phase(b: dict) -> int:
    if b.get("confirmed") is True:
        return 5
    if b.get("service"):
        return 4
    if b.get("doctor_name") is not None or b.get("any_doctor"):
        return 3
    if b.get("insurance_name") is not None:
        return 2
    return 1


def update_booking(booking: dict, msg: ToolMessage) -> None:
    try:
        data = json.loads(msg.content)
    except Exception:
        return
    if not isinstance(data, dict) or data.get("status") != "ok":
        return

    name = getattr(msg, "name", None)
    if name == "get_insurance_id_by_insurance_name":
        if data.get("mode") == "private":
            booking["insurance_id"] = None
            booking["insurance_name"] = "PRIVATO"
        else:
            booking["insurance_id"] = data.get("insurance_id")
            booking["insurance_name"] = data.get("name")
    elif name == "search_doctor_names":
        # Dual-use tool: silent INIT (no args) returns the {id: name} map
        # only — no `mode` key — and is ignored here. An explicit pick call
        # (with picked_name) attaches a `mode` key, which is the signal we
        # commit on.
        mode = data.get("mode")
        if mode == "any_doctor":
            booking["any_doctor"] = True
            booking["doctor_name"] = None
            booking["doctor_id"] = None
        elif mode == "picked" and data.get("doctor"):
            booking["doctor_name"] = data["doctor"]
            booking["doctor_id"] = data.get("doctor_id")
            booking["any_doctor"] = False
    elif name in {"search_dates", "get_new_dates"}:
        new_service = data.get("service")
        if new_service:
            old = booking.get("service") or {}
            if new_service.get("activityid") != old.get("activityid"):
                booking["slot"] = None
                booking["confirmed"] = None
            booking["service"] = new_service
    elif name == "book_appointment":
        booking["slot"] = data.get("booking")
        booking["confirmed"] = True
    elif name == "request_deferred_appointment":
        booking["slot"] = data.get("booking")
        booking["confirmed"] = True


def format_booking_snapshot(booking: dict, user: dict | None = None) -> str:
    phase = current_phase(booking)
    now   = datetime.now().strftime("%A, %d %B %Y — %H:%M")

    def tick(v: object) -> str:
        return "✓" if v else "⏳"

    # ── User row (authentication is upstream of this agent) ───────────────
    # The router fires a silent phone lookup at session start and injects the
    # result here as `user`. The four cases below cover the auth outcomes.
    auth_status = (user or {}).get("status")
    if user and user.get("userid"):
        # Profile hints: preferred insurance / area, plus past reservations
        # pre-loaded by the router (see ensure_caller_auth), so the model can
        # bias toward "the doctor / insurance from last time".
        hints = []
        if user.get("insuranceid"):
            hints.append(f"caller_insuranceid={user.get('insuranceid')} "
                         "(propose this in phase 1 if it appears in search_insurance_names)")
        if user.get("areaid"):
            hints.append(f"caller_areaid={user.get('areaid')} "
                         "(propose this site if it appears in search_areas)")
        past = user.get("past_reservations") or []
        if past:
            preview = "; ".join(
                f"{r.get('Date','')} {r.get('Type','').strip()} dr.{r.get('Doctor','')}"
                f" @ {r.get('Area','')}" for r in past[:3]
            )
            hints.append(f"past_reservations={len(past)} (bias toward usual doctor/insurance; latest: {preview})")
        hint_str = ("\n                  · " + "\n                  · ".join(hints)) if hints else ""
        user_row = (
            f"{user.get('name','')} {user.get('surname','')} "
            f"(userid={user.get('userid')}, phone={user.get('phone','—')}) "
            "→ AUTHENTICATED, greet by first name, skip identity questions."
            + hint_str
        )
    elif auth_status == "ambiguous":
        cands = user.get("candidates", []) or []
        user_row = (
            f"AMBIGUOUS — {len(cands)} profiles match phone={user.get('phone','—')}. "
            "→ ask for date of birth and call authenticate_user_by_birthdate("
            "phone=<caller>, birthdate=DD/MM/YYYY)."
        )
    elif auth_status == "not_found":
        user_row = (
            f"NOT FOUND for phone={user.get('phone','—')}. "
            "→ ask for codice fiscale and call authenticate_user_by_codice_fiscale; "
            "if still no match, offer transfer_to_flow('patient_registration', …)."
        )
    elif auth_status in {"invalid_phone", "error"}:
        user_row = (
            f"AUTH ERROR ({auth_status}). Proceed as anonymous; on book_appointment "
            "fall back to CAREDESK_TEST_USER_ID or escalate to registration."
        )
    else:
        user_row = (
            "anonymous (no caller phone) → "
            "ask for phone and call authenticate_user_by_phone before booking; "
            "fallback on settings.caredesk_test_user_id only for dev sessions."
        )

    # ── Insurance ─────────────────────────────────────────────────────────
    ins_name = booking.get("insurance_name")
    ins_id   = booking.get("insurance_id")
    if ins_name == "PRIVATO":
        ins = "PRIVATO  → tool calls: pass insuranceid=null"
    elif ins_name:
        ins = f"{ins_name}  (insuranceid={ins_id})"
    else:
        ins = None

    # ── Doctor ────────────────────────────────────────────────────────────
    doc_name = booking.get("doctor_name")
    doc_id   = booking.get("doctor_id")
    if booking.get("any_doctor"):
        doc = "any (no preference)  → tool calls: pass resourceid=null"
    elif doc_name:
        doc = f"{doc_name}  (resourceid={doc_id})"
    else:
        doc = None

    # ── Service / slot ────────────────────────────────────────────────────
    svc  = booking.get("service")
    slot = booking.get("slot")
    conf = booking.get("confirmed")
    deferred = bool((svc or {}).get("mopBookability") == "DEFERRED_EMAIL")
    svc_str = (
        f"{svc.get('typology','')} — {svc.get('activity','')}  "
        f"(activityid={svc.get('activityid')}{' DEFERRED' if deferred else ''})"
        if svc else None
    )
    slot_str = (
        f"{slot.get('start_date','')} {slot.get('startTime','')} — Dr {slot.get('doctor_name','')}"
        if slot else None
    )

    return "\n".join([
        "━━━ LAB_BOOKING STATE (internal — NEVER narrate to patient) ━━━",
        f"  today         : {now}",
        f"  user          : {user_row}",
        f"  current_phase : {phase} ({PHASE_NAMES[phase]})",
        "  ─── collected ──────────────────────────────────────────",
        f"  {tick(ins)}  insurance    : {ins or 'pending'}",
        f"  {tick(doc)}  doctor       : {doc or 'pending'}",
        f"  {tick(svc_str)}  service      : {svc_str or 'pending'}",
        f"  {tick(slot_str)}  slot         : {slot_str or 'pending'}",
        f"  {tick(conf is True)}  confirmed    : {conf if conf is not None else 'pending'}",
        "  ─── state-only notes (no scripting) ────────────────────",
        f"  · state          : phase {phase} = {PHASE_NAMES[phase]}",
        f"  · tool guardrail : {_PHASE_GATE[phase]}",
        "  · phrasing       : the 'Dialogo flessibile' rules from the system prompt govern HOW to talk. "
        "If the patient asks a question, answer it first; vary the phrasing of phase questions, never repeat them verbatim.",
        "  · already done   : do not re-call tools for fields already marked ✓ above.",
        (
            "  · weboff mode    : ON — some doctors/services may carry a `weboff=true` flag; never propose them, and on patient mention say they require operator or web portal."
            if settings.caredesk_manage_weboff
            else "  · weboff mode    : off (only fully bookable entries are exposed)"
        ),
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ])
