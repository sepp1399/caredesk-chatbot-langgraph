"""Patient identification — three independent strategies.

  1) phone               default, fastest; fired silently by the router
                         (ensure_caller_auth) using the chat caller-ID.
  2) codice fiscale      fallback when phone matched zero profiles —
                         exposed to the agents as `authenticate_user_by_codice_fiscale`.
  3) date of birth       disambiguator when phone matched multiple
                         profiles — exposed as `authenticate_user_by_birthdate`.

Plus helpers consumed directly by the router (no LLM round-trip):
  - `lookup_caller_by_phone`           — phone lookup at session start.
  - `lookup_caller_by_idnumber`        — CF lookup helper.
  - `disambiguate_candidates_by_birthdate` — client-side filter.
"""

from __future__ import annotations

from datetime import datetime

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.integrations.caredesk import cd_get
from app.tools.shared._helpers import _err, _log, _ok, is_valid_codice_fiscale


async def _lookup_user_by(params: dict) -> list[dict]:
    """Look up users via GET /users.

    Both `userid` and `memberid` are read so downstream tools can resolve
    the patient regardless of which key the backend returns. `insuranceid`
    and `areaid` are pulled too: they seed the patient's preferred insurance
    and site as hints during booking.
    """
    data = await cd_get("/users", {
        "profile":          "EXTERNAL_USER",
        "exclude_children": "ON",
        **params,
        "fields":           "userid,memberid,name,surname,birthday,phone,hometel,insuranceid,areaid",
    })
    return data.get("results", []) or []


def _resolve_userid(u: dict) -> str | None:
    return u.get("userid") or u.get("memberid")


async def lookup_caller_by_phone(phone: str | None) -> dict:
    """Pre-flight phone auth — fired silently at the start of every
    conversation using the caller-ID provided by the channel.

    Unlike the @tool wrapper (which returns a JSON string for the LLM), this
    helper returns a structured dict suitable for the router to inject into
    the agent state. The return shape is:

      {"status": "authenticated",  "userid", "name", "surname", "phone"}
      {"status": "ambiguous",      "candidates": [...],            "phone"}
      {"status": "not_found",                                       "phone"}
      {"status": "invalid_phone",                                   "phone"}
      {"status": "error",          "message",                       "phone"}
    """
    norm = "".join(c for c in (phone or "") if c.isdigit())
    if len(norm) < 6:
        return {"status": "invalid_phone", "phone": norm}
    try:
        results = await _lookup_user_by({"phone": norm})
    except Exception as exc:
        return {"status": "error", "message": str(exc), "phone": norm}

    if not results:
        return {"status": "not_found", "phone": norm}
    if len(results) == 1:
        u = results[0]
        return {
            "status":      "authenticated",
            "userid":      _resolve_userid(u),
            "name":        u.get("name"),
            "surname":     u.get("surname"),
            "phone":       norm,
            # Profile-level hints: the patient's preferred insurance / area,
            # surfaced later only when present in the bookable list.
            "insuranceid": u.get("insuranceid") or None,
            "areaid":      u.get("areaid") or None,
        }
    return {
        "status":     "ambiguous",
        "phone":      norm,
        "candidates": [
            {"userid": _resolve_userid(u),
             "name":    u.get("name"),
             "surname": u.get("surname"),
             "birthday": u.get("birthday") or u.get("birthdate")}
            for u in results[:5]
        ],
    }


async def lookup_caller_by_idnumber(idnumber: str | None) -> dict:
    """Codice-fiscale fallback in the patient-identification cascade.

    Same return shape as `lookup_caller_by_phone`; used by the router when
    the phone lookup failed (`status=not_found` or `invalid_phone`) and
    the patient provides their codice fiscale instead.
    """
    cf = (idnumber or "").upper().strip()
    if not is_valid_codice_fiscale(cf):
        return {"status": "invalid_idnumber", "idnumber": cf}
    try:
        results = await _lookup_user_by({"idnumber": cf})
    except Exception as exc:
        return {"status": "error", "message": str(exc), "idnumber": cf}

    if not results:
        return {"status": "not_found", "idnumber": cf}
    u = results[0]
    return {
        "status":      "authenticated",
        "userid":      _resolve_userid(u),
        "name":        u.get("name"),
        "surname":     u.get("surname"),
        "idnumber":    cf,
        "phone":       u.get("phone"),
        "insuranceid": u.get("insuranceid") or None,
        "areaid":      u.get("areaid") or None,
    }


def disambiguate_candidates_by_birthdate(candidates: list[dict],
                                         birthdate: str | None) -> dict:
    """Client-side filter on a previously-loaded set of ambiguous candidates.

    Filters the users obtained from the phone lookup, matching `birthday`
    against `DD/MM/YYYY`. Returns:
        {status: "authenticated", userid, name, surname}            if 1 match
        {status: "still_ambiguous", candidates}                     if >1 match
        {status: "not_found"}                                       if 0 matches
        {status: "invalid_birthdate"}                               if format invalid
    """
    target = (birthdate or "").strip()
    try:
        datetime.strptime(target, "%d/%m/%Y")
    except ValueError:
        return {"status": "invalid_birthdate", "birthdate": target}

    matches = [
        c for c in (candidates or [])
        if (c.get("birthday") or c.get("birthdate") or "") == target
    ]
    if not matches:
        return {"status": "not_found", "birthdate": target}
    if len(matches) > 1:
        return {"status": "still_ambiguous", "candidates": matches}
    u = matches[0]
    return {
        "status":  "authenticated",
        "userid":  u.get("userid") or u.get("memberid"),
        "name":    u.get("name"),
        "surname": u.get("surname"),
        "phone":   u.get("phone"),
        "birthday": target,
    }


async def load_caller_past_reservations(userid: str | None,
                                        pager_limit: int = 5) -> list[dict]:
    """Pull the patient's recent past reservations.

    Queries `GET /reservations?pastRes=true` for the completed / confirmed /
    approved states, then formats the result as
        [{'Type', 'Doctor', 'Insurance', 'Area', 'Date'}, ...]
    and injects it into the agent state so the model can bias toward "the
    doctor / insurance from last time".

    Returns the formatted list (max `pager_limit` entries). Empty list on
    error or when no userid is provided.
    """
    if not userid:
        return []
    try:
        data = await cd_get("/reservations", {
            "pastRes":      "true",
            # Completed (3), user-confirmed (2) and approved (0) states.
            "is_pending[]": [3, 2, 0],
            "userids[]":    [userid],
            "orders[]":     ["start_date", "startTime"],
            "orderWay":     "DESC",
            "fields":       "name,typologyTitle,activityTitle,insuranceTitle,areaTitle,start_date",
            "pager_limit":  pager_limit,
        })
    except Exception:
        return []

    items = data.get("results") or []
    return [
        {
            "Type":      f"{r.get('typologyTitle','')} {r.get('activityTitle','')}".strip(),
            "Doctor":    r.get("name", "") or "",
            "Insurance": r.get("insuranceTitle", "") or "",
            "Area":      r.get("areaTitle", "") or "",
            "Date":      r.get("start_date", "") or "",
        }
        for r in items
    ]


# ── LLM-facing tools ────────────────────────────────────────────────────────
# The router's pre-flight runs the phone lookup automatically using the
# chat caller-ID; these tools cover the cases where the agent has to
# resolve identification mid-flow (caller dictates a different phone,
# falls back to CF, or disambiguates by birthdate).


class _PhoneIn(BaseModel):
    phone: str = Field(description="E.164-style phone, digits only. Country prefix optional.")


@tool(args_schema=_PhoneIn)
async def authenticate_user_by_phone(phone: str) -> str:
    """
    Identify the patient by phone number.

    Calling rules:
    - Call only after the patient has dictated/typed a phone in the current
      turn (not from past sessions).
    - On exact match (1 result): returns userid → caller is authenticated.
    - On multiple matches: returns ambiguous list — ask for date of birth
      and then call `authenticate_user_by_birthdate`.
    - On no match: switch to `authenticate_user_by_codice_fiscale`
      or offer registration / lead.
    """
    inputs = {"phone": phone}
    norm = "".join(c for c in (phone or "") if c.isdigit())
    if len(norm) < 6:
        return _log("authenticate_user_by_phone", inputs,
                    _err("invalid_phone", "Phone too short — ask the patient to repeat."))
    try:
        results = await _lookup_user_by({"phone": norm})
    except Exception as exc:
        return _log("authenticate_user_by_phone", inputs, _err("backend_error", str(exc)))

    if not results:
        return _log("authenticate_user_by_phone", inputs,
                    _err("not_found",
                         "No user found for this phone — try codice fiscale or offer registration."))
    if len(results) == 1:
        u = results[0]
        return _log("authenticate_user_by_phone", inputs,
                    _ok(method="phone", userid=_resolve_userid(u), name=u.get("name"),
                        surname=u.get("surname")))
    return _log("authenticate_user_by_phone", inputs,
                _ok(method="phone", ambiguous=True, count=len(results),
                    candidates=[{"userid": _resolve_userid(u), "name": u.get("name"),
                                 "surname": u.get("surname")} for u in results[:5]]))


class _CFIn(BaseModel):
    codice_fiscale: str = Field(
        description=(
            "Italian Codice Fiscale (16 alphanumeric chars: "
            "6 letters + 2 digits + 1 letter + 2 digits + 1 letter + 3 digits + 1 letter)."
        )
    )


@tool(args_schema=_CFIn)
async def authenticate_user_by_codice_fiscale(codice_fiscale: str) -> str:
    """
    Identify the patient by Italian Codice Fiscale (fallback after phone).

    The tool validates the format locally before hitting CareDesk; if the
    pattern doesn't match, returns `invalid_format` and the agent should
    re-prompt instead of guessing.
    """
    inputs = {"codice_fiscale": codice_fiscale}
    cf = (codice_fiscale or "").upper().strip()
    if not is_valid_codice_fiscale(cf):
        return _log("authenticate_user_by_codice_fiscale", inputs,
                    _err("invalid_format",
                         "CF must be 16 alphanumeric chars — re-ask the patient."))
    try:
        results = await _lookup_user_by({"idnumber": cf})
    except Exception as exc:
        return _log("authenticate_user_by_codice_fiscale", inputs,
                    _err("backend_error", str(exc)))

    if not results:
        return _log("authenticate_user_by_codice_fiscale", inputs,
                    _err("not_found",
                         "No user found for this CF — offer registration or transfer to a human."))
    u = results[0]
    return _log("authenticate_user_by_codice_fiscale", inputs,
                _ok(method="codice_fiscale", userid=_resolve_userid(u),
                    name=u.get("name"), surname=u.get("surname")))


class _BirthdateIn(BaseModel):
    phone: str     = Field(description="Phone the patient already provided.")
    birthdate: str = Field(description="Date of birth in DD/MM/YYYY.")


@tool(args_schema=_BirthdateIn)
async def authenticate_user_by_birthdate(phone: str, birthdate: str) -> str:
    """
    Disambiguate among multiple users that share the same phone number.

    Called AFTER `authenticate_user_by_phone` returned `ambiguous=true`.
    Loads the candidates by phone and filters locally on `birthday`
    (DD/MM/YYYY).
    """
    inputs = {"phone": phone, "birthdate": birthdate}
    norm = "".join(c for c in (phone or "") if c.isdigit())
    if len(norm) < 6:
        return _log("authenticate_user_by_birthdate", inputs,
                    _err("invalid_phone", "Phone too short — ask the patient to repeat."))

    target = (birthdate or "").strip()
    try:
        datetime.strptime(target, "%d/%m/%Y")
    except ValueError:
        return _log("authenticate_user_by_birthdate", inputs,
                    _err("invalid_birthdate", "Use DD/MM/YYYY."))

    try:
        candidates = await _lookup_user_by({"phone": norm})
    except Exception as exc:
        return _log("authenticate_user_by_birthdate", inputs,
                    _err("backend_error", str(exc)))

    if not candidates:
        return _log("authenticate_user_by_birthdate", inputs,
                    _err("not_found", "No user matches the phone number."))

    matches = [u for u in candidates
               if (u.get("birthday") or u.get("birthdate") or "") == target]
    if not matches:
        return _log("authenticate_user_by_birthdate", inputs,
                    _err("not_found", "No user matches phone + birthdate."))
    if len(matches) > 1:
        return _log("authenticate_user_by_birthdate", inputs,
                    _err("still_ambiguous",
                         "Multiple users match — transfer to a human operator."))
    u = matches[0]
    return _log("authenticate_user_by_birthdate", inputs,
                _ok(method="phone+birthdate", userid=_resolve_userid(u),
                    name=u.get("name"), surname=u.get("surname")))
