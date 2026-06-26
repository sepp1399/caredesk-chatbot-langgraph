"""In-memory CareDesk booking backend (mock).

This module replaces what used to be a live REST integration with a
self-contained, in-memory fake so the whole assistant runs standalone with
nothing but a Gemini API key — no external tenant, bearer token or network
access required.

It deliberately keeps the *same call surface and response envelope* the
tools were written against, so none of the tool logic had to change:

  - `cd_get` / `cd_post` unwrap to `body["return"]` (the common case).
  - `cd_*_envelope` variants return the full envelope so callers that need
    `additional_return` (booking, reschedule) can read it.

Every "endpoint" is served from the seed fixtures below; write operations
(book / reschedule / cancel / register / lead) mutate process-local state
so a conversation stays coherent within a running server.
"""

from __future__ import annotations

import itertools
import logging
import random
import string
from datetime import datetime, timedelta
from typing import Any

from app.config import settings

logger = logging.getLogger("caredesk_lg.caredesk")

# ──────────────────────────────────────────────────────────────────────────────
# Seed fixtures
# ──────────────────────────────────────────────────────────────────────────────

# mopBookability is "ON" (self-bookable) or "DEFERRED_EMAIL" (request only).
# Anything else is filtered out by the insurance/activity tools.
_INSURANCES: list[dict] = [
    {"insuranceid": "ins_allianz",   "insurance_title": "Allianz",  "mopBookability": "ON"},
    {"insuranceid": "ins_unisalute", "insurance_title": "UniSalute","mopBookability": "ON"},
    {"insuranceid": "ins_generali",  "insurance_title": "Generali", "mopBookability": "ON"},
    {"insuranceid": "ins_ssn",       "insurance_title": "SSN",      "mopBookability": "ON"},
    # Non-bookable — present in the catalog but filtered by the tools.
    {"insuranceid": "ins_fondoest",  "insurance_title": "Fondo Est","mopBookability": "OFF"},
]

# isHidden=1 doctors only surface in weboff mode (CAREDESK_MANAGE_WEBOFF=true).
_DOCTORS: list[dict] = [
    {"resourceid": "doc_rossi",   "name": "DOTT. Mario Rossi",     "isHidden": 0},
    {"resourceid": "doc_bianchi", "name": "DOTT.SSA Anna Bianchi", "isHidden": 0},
    {"resourceid": "doc_verdi",   "name": "DR. Luca Verdi",        "isHidden": 0},
    {"resourceid": "doc_ferrari", "name": "DOTT. Giulia Ferrari",  "isHidden": 0},
    {"resourceid": "doc_neri",    "name": "DOTT. Paolo Neri",      "isHidden": 1},
]

# act_rmn is DEFERRED_EMAIL → exercises the deferred-request branch.
# act_nutrizione is hidden → only shown in weboff mode, never bookable here.
_ACTIVITIES: list[dict] = [
    {"activityid": "act_cardio", "activityTitle": "Visita cardiologica",
     "typologyid": "typ_visita", "typologyTitle": "Visita specialistica",
     "mopBookability": "ON", "isHidden": 0, "price": "120.00€"},
    {"activityid": "act_eco_addome", "activityTitle": "Ecografia addominale",
     "typologyid": "typ_eco", "typologyTitle": "Ecografia",
     "mopBookability": "ON", "isHidden": 0, "price": "95.00€"},
    {"activityid": "act_sangue", "activityTitle": "Esame del sangue",
     "typologyid": "typ_lab", "typologyTitle": "Analisi di laboratorio",
     "mopBookability": "ON", "isHidden": 0, "price": "45.00€"},
    {"activityid": "act_rmn", "activityTitle": "Risonanza magnetica",
     "typologyid": "typ_imaging", "typologyTitle": "Diagnostica per immagini",
     "mopBookability": "DEFERRED_EMAIL", "isHidden": 0, "price": "210.00€"},
    {"activityid": "act_nutrizione", "activityTitle": "Visita nutrizionale",
     "typologyid": "typ_visita", "typologyTitle": "Visita specialistica",
     "mopBookability": "ON", "isHidden": 1, "price": "90.00€"},
]

# Single site keeps the booking happy-path frictionless (the area tool
# returns it directly instead of asking the patient to choose).
_AREAS: list[dict] = [
    {"areaid": "area_centro", "areaTitle": "Ospedale Salus — Sede Centro",
     "city": "Torino", "province": "TO", "address": "Via Roma 10, Torino"},
]

# Demo patients. usr_demo_001 is the default authenticated caller
# (CAREDESK_TEST_USER_ID) — its phone resolves at session start.
_USERS: list[dict] = [
    {
        "userid": "usr_demo_001", "memberid": "usr_demo_001",
        "name": "Marco", "surname": "Esposito",
        "birthday": "12/05/1985",
        "phone": "3331234567", "hometel": "0110001122",
        "idnumber": "SPSMRC85E12L219X",
        "email": "marco.esposito@example.com",
        "has_account": True,
        "insuranceid": "ins_unisalute", "areaid": "area_centro",
    },
    {
        "userid": "usr_demo_002", "memberid": "usr_demo_002",
        "name": "Laura", "surname": "Bianchi",
        "birthday": "03/09/1990",
        "phone": "3339876543", "hometel": "",
        "idnumber": "BNCLRA90P43L219Y",
        "email": "laura.bianchi@example.com",
        "has_account": False,
        "insuranceid": "ins_allianz", "areaid": "area_centro",
    },
]


def _today() -> datetime:
    return datetime.now()


def _fmt(d: datetime) -> str:
    return d.strftime("%d/%m/%Y")


# Reservation store, keyed by resid. Seeded with one upcoming + one past
# reservation for the default caller so the manage flow has something to act
# on and the auth hint ("doctor from last time") is populated.
_RESERVATIONS: dict[str, dict] = {
    "res_seed_future": {
        "resid": "res_seed_future", "userid": "usr_demo_001",
        "activityid": "act_cardio", "activityTitle": "Visita cardiologica",
        "typologyTitle": "Visita specialistica",
        "resourceid": "doc_rossi", "resourceName": "Mario Rossi", "name": "Mario Rossi",
        "areaid": "area_centro", "areaTitle": "Ospedale Salus — Sede Centro",
        "insuranceid": "ins_unisalute", "insuranceTitle": "UniSalute",
        "start_date": _fmt(_today() + timedelta(days=7)),
        "startTime": "10:30", "endTime": "11:00",
        "is_cancellable": 1, "is_reschedulable": 1, "is_pending": 0,
        "future": True,
    },
    "res_seed_past": {
        "resid": "res_seed_past", "userid": "usr_demo_001",
        "activityid": "act_sangue", "activityTitle": "Esame del sangue",
        "typologyTitle": "Analisi di laboratorio",
        "resourceid": "doc_bianchi", "resourceName": "Anna Bianchi", "name": "Anna Bianchi",
        "areaid": "area_centro", "areaTitle": "Ospedale Salus — Sede Centro",
        "insuranceid": "ins_unisalute", "insuranceTitle": "UniSalute",
        "start_date": _fmt(_today() - timedelta(days=30)),
        "startTime": "08:00", "endTime": "08:15",
        "is_cancellable": 0, "is_reschedulable": 0, "is_pending": 3,
        "future": False,
    },
}

_id_counter = itertools.count(1)


def _next_id(prefix: str) -> str:
    return f"{prefix}_{next(_id_counter):04d}"


def _token(n: int = 12) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _digits(value: Any) -> str:
    if value is None:
        return ""
    return "".join(c for c in str(value) if c.isdigit())


# ──────────────────────────────────────────────────────────────────────────────
# Lookup helpers
# ──────────────────────────────────────────────────────────────────────────────

def _doctor_name(resourceid: str | None) -> str:
    raw = next((d["name"] for d in _DOCTORS if d["resourceid"] == resourceid), "")
    # Strip the Italian title prefix so the stored name is clean.
    for pre in ("DOTT.SSA", "DOTT.", "DOTT", "DR.", "DR"):
        if raw.upper().startswith(pre):
            raw = raw[len(pre):]
            break
    return raw.strip().title()


def _activity(activityid: str | None) -> dict | None:
    return next((a for a in _ACTIVITIES if a["activityid"] == activityid), None)


def _area(areaid: str | None) -> dict:
    return next((a for a in _AREAS if a["areaid"] == areaid), _AREAS[0])


def _insurance_title(insuranceid: str | None) -> str:
    return next((i["insurance_title"] for i in _INSURANCES
                 if i["insuranceid"] == insuranceid), "")


def _price(activityid: str | None) -> str:
    a = _activity(activityid)
    return a.get("price", "") if a else ""


# ──────────────────────────────────────────────────────────────────────────────
# Read endpoints
# ──────────────────────────────────────────────────────────────────────────────

_SLOT_TIMES = ["09:00", "10:30", "12:00", "15:00", "16:30", "18:00"]


def _gen_availabilities(params: dict) -> list[dict]:
    """Synthesise upcoming slots for the requested activity/doctor/area."""
    activityid = params.get("activityid") or ""
    activity = _activity(activityid) or {}
    insuranceid = params.get("insuranceid")
    areaid = params.get("areaid") or _AREAS[0]["areaid"]
    area = _area(areaid)
    requested_doctor = params.get("resourceid")
    start_time = params.get("startTime")

    try:
        max_results = int(params.get("maxResults") or 6)
    except (TypeError, ValueError):
        max_results = 6

    start_param = params.get("start_date")
    try:
        start_day = datetime.strptime(start_param, "%d/%m/%Y") if start_param else _today() + timedelta(days=1)
    except ValueError:
        start_day = _today() + timedelta(days=1)

    # Rotate doctors when the patient has no preference, otherwise pin the one
    # they chose.
    bookable_doctors = [d["resourceid"] for d in _DOCTORS if not d["isHidden"]]
    doctor_cycle = itertools.cycle([requested_doctor] if requested_doctor else bookable_doctors)

    slots: list[dict] = []
    day = start_day
    while len(slots) < max_results:
        if day.weekday() == 6:  # skip Sundays
            day += timedelta(days=1)
            continue
        for t in _SLOT_TIMES:
            if start_time and t < start_time and day.date() == start_day.date():
                continue
            if len(slots) >= max_results:
                break
            rid = next(doctor_cycle)
            end = (datetime.strptime(t, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
            slots.append({
                "slotid":              _next_id("slot"),
                "start_date":          _fmt(day),
                "end_date":            _fmt(day),
                "startTime":           t,
                "endTime":             end,
                "resourceid":          rid,
                "resourceName":        _doctor_name(rid),
                "activityid":          activityid,
                "activityTitle":       activity.get("activityTitle", ""),
                "activityPrice":       activity.get("price", ""),
                "typologyid":          activity.get("typologyid", ""),
                "typologyTitle":       activity.get("typologyTitle", ""),
                "areaid":              area["areaid"],
                "areaTitle":           area["areaTitle"],
                "address":             area["address"],
                "city":                area["city"],
                "province":            area["province"],
                "insuranceid":         insuranceid or "",
                "insurance_title":     _insurance_title(insuranceid),
                "provider_session_id": _token(),
                "searchid":            _token(),
            })
        day += timedelta(days=1)
    return slots


def _query_users(params: dict) -> list[dict]:
    phone = _digits(params.get("phone"))
    hometel = _digits(params.get("hometel"))
    idnumber = (params.get("idnumber") or "").upper().strip()
    out: list[dict] = []
    for u in _USERS:
        if phone and _digits(u.get("phone")) == phone:
            out.append(u)
        elif hometel and _digits(u.get("hometel")) == hometel:
            out.append(u)
        elif idnumber and (u.get("idnumber") or "").upper() == idnumber:
            out.append(u)
    return out


def _parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%d/%m/%Y")
    except (ValueError, TypeError):
        return datetime.max


def _query_reservations(params: dict) -> list[dict]:
    userids = params.get("userids[]") or params.get("userids") or []
    if isinstance(userids, str):
        userids = [userids]
    want_future = str(params.get("futureRes", "")).lower() == "true"
    want_past = str(params.get("pastRes", "")).lower() == "true"
    pending = params.get("is_pending[]") or params.get("is_pending")

    out: list[dict] = []
    for r in _RESERVATIONS.values():
        if userids and r.get("userid") not in userids:
            continue
        if want_future and not r.get("future"):
            continue
        if want_past and r.get("future"):
            continue
        if pending is not None:
            allowed = pending if isinstance(pending, (list, tuple)) else [pending]
            try:
                if int(r.get("is_pending", 0)) not in [int(x) for x in allowed]:
                    continue
            except (TypeError, ValueError):
                pass
        out.append({k: v for k, v in r.items() if k not in {"future"}})

    out.sort(key=lambda r: (_parse_date(r.get("start_date", "")), r.get("startTime", "")))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Write endpoints
# ──────────────────────────────────────────────────────────────────────────────

def _create_user(body: dict) -> str:
    uid = _next_id("usr")
    _USERS.append({
        "userid": uid, "memberid": uid,
        "name": body.get("fname", ""), "surname": body.get("lname", ""),
        "birthday": body.get("birthday", ""),
        "phone": _digits(body.get("phone")), "hometel": "",
        "idnumber": (body.get("idnumber") or "").upper(),
        "email": body.get("email", ""),
        "has_account": True,
        "insuranceid": None, "areaid": "area_centro",
    })
    logger.info("Mock register_user → %s", uid)
    return uid


def _create_lead(body: dict) -> str:
    leadid = _next_id("lead")
    logger.info("Mock create_lead → %s (%s)", leadid, body.get("name"))
    return leadid


def _reservation_detail(resid: str, body: dict) -> dict:
    return {
        "start_date":   body.get("start_date", ""),
        "startTime":    body.get("startTime", ""),
        "endTime":      body.get("endTime", ""),
        "resourceName": _doctor_name(body.get("resourceid")),
        "areaTitle":    _area(body.get("areaid")).get("areaTitle", ""),
        "price_end":    _price(body.get("activityid")),
        "status_string": "Confermato",
        "status_code":  "0",
        "checkin_code": _token(6).upper(),
    }


def _create_reservation(body: dict) -> dict:
    resid = _next_id("res")
    activity = _activity(body.get("activityid")) or {}
    area = _area(body.get("areaid"))
    detail = _reservation_detail(resid, body)
    _RESERVATIONS[resid] = {
        "resid": resid, "userid": body.get("userid"),
        "activityid": body.get("activityid", ""),
        "activityTitle": activity.get("activityTitle", ""),
        "typologyTitle": activity.get("typologyTitle", ""),
        "resourceid": body.get("resourceid", ""),
        "resourceName": detail["resourceName"], "name": detail["resourceName"],
        "areaid": area["areaid"], "areaTitle": area["areaTitle"],
        "insuranceid": body.get("insuranceid", ""),
        "insuranceTitle": _insurance_title(body.get("insuranceid")),
        "start_date": body.get("start_date", ""),
        "startTime": body.get("startTime", ""), "endTime": body.get("endTime", ""),
        "is_cancellable": 1, "is_reschedulable": 1, "is_pending": 0,
        "future": True,
    }
    logger.info("Mock book_appointment → %s", resid)
    return {"result": "OK", "return": resid,
            "additional_return": {"reservations": {resid: detail}}}


def _create_deferred(endpoint: str, body: dict) -> dict:
    defid = _next_id("defres")
    logger.info("Mock deferred request → %s (%s)", defid, body.get("activityid"))
    return {"result": "OK", "return": defid}


def _update_reservation(resid: str, body: dict) -> dict:
    detail = _reservation_detail(resid, body)
    existing = _RESERVATIONS.get(resid)
    if existing is not None:
        existing.update({
            "start_date": body.get("start_date", existing.get("start_date")),
            "startTime": body.get("startTime", existing.get("startTime")),
            "endTime": body.get("endTime", existing.get("endTime")),
            "resourceid": body.get("resourceid", existing.get("resourceid")),
            "resourceName": detail["resourceName"] or existing.get("resourceName"),
        })
    logger.info("Mock reschedule_reservation → %s", resid)
    return {"result": "OK", "return": resid,
            "additional_return": {"reservations": {resid: detail}}}


def _delete_reservation(resid: str) -> dict:
    _RESERVATIONS.pop(resid, None)
    logger.info("Mock cancel_reservation → %s", resid)
    return {"result": "OK", "return": "OK"}


# ──────────────────────────────────────────────────────────────────────────────
# Dispatcher + envelope helpers (same contract the tools were written against)
# ──────────────────────────────────────────────────────────────────────────────

class CareDeskApiError(RuntimeError):
    """Raised when the mock backend cannot serve a request."""


def _ok_return(ret: Any) -> dict:
    return {"result": "OK", "return": ret}


def _route(method: str, endpoint: str, params: dict, body: dict) -> dict:
    m = method.upper()
    if m == "GET":
        if endpoint == "/insurances":
            return _ok_return({"results": list(_INSURANCES)})
        if endpoint == "/resources":
            return _ok_return({"results": list(_DOCTORS)})
        if endpoint == "/activities":
            return _ok_return({"results": list(_ACTIVITIES)})
        if endpoint == "/areas":
            return _ok_return({"results": list(_AREAS)})
        if endpoint == "/availabilities":
            return _ok_return({"results": {"availabilities": _gen_availabilities(params)}})
        if endpoint == "/users":
            return _ok_return({"results": _query_users(params)})
        if endpoint == "/reservations":
            return _ok_return({"results": _query_reservations(params)})
    elif m == "POST":
        if endpoint == "/users":
            return _ok_return({"userid": _create_user(body)})
        if endpoint == "/leads":
            return _ok_return({"leadid": _create_lead(body)})
        if endpoint == "/reservations":
            return _create_reservation(body)
        if endpoint.startswith("/activities/") and endpoint.endswith("/_apprequest"):
            return _create_deferred(endpoint, body)
    elif m == "PUT":
        if endpoint.startswith("/reservations/"):
            return _update_reservation(endpoint.rsplit("/", 1)[-1], body)
    elif m == "DELETE":
        if endpoint.startswith("/reservations/"):
            return _delete_reservation(endpoint.rsplit("/", 1)[-1])
    raise CareDeskApiError(f"No mock handler for {m} {endpoint}")


def _check_ok(body: Any) -> dict:
    if not isinstance(body, dict):
        raise CareDeskApiError(f"Unexpected response payload type: {type(body).__name__}")
    if "result" in body and body.get("result") != "OK":
        msg = body.get("msg") or body.get("exception") or "unknown"
        raise CareDeskApiError(f"CareDesk API error: {msg}")
    return body


def _unwrap(body: Any) -> dict:
    checked = _check_ok(body)
    if "result" in checked and "return" in checked:
        ret = checked["return"]
        return ret if isinstance(ret, dict) else {"return": ret}
    return checked


def _clean_params(params: dict | None) -> dict:
    return {k: v for k, v in (params or {}).items() if v is not None and v != ""}


async def _request(method: str, endpoint: str, *,
                   params: dict | None = None,
                   json_body: dict | None = None) -> dict:
    return _route(method, endpoint, _clean_params(params), json_body or {})


async def aclose() -> None:
    """No-op — kept so the FastAPI lifespan shutdown hook stays unchanged."""
    return None


async def cd_get(endpoint: str, params: dict | None = None) -> dict:
    return _unwrap(await _request("GET", endpoint, params=params))


async def cd_get_envelope(endpoint: str, params: dict | None = None) -> dict:
    return _check_ok(await _request("GET", endpoint, params=params))


async def cd_post(endpoint: str, body: dict, params: dict | None = None) -> dict:
    return _unwrap(await _request("POST", endpoint, params=params, json_body=body))


async def cd_post_envelope(endpoint: str, body: dict,
                           params: dict | None = None) -> dict:
    return _check_ok(await _request("POST", endpoint, params=params, json_body=body))


async def cd_put_envelope(endpoint: str, body: dict,
                          params: dict | None = None) -> dict:
    return _check_ok(await _request("PUT", endpoint, params=params, json_body=body))


async def cd_delete_envelope(endpoint: str, params: dict | None = None) -> dict:
    return _check_ok(await _request("DELETE", endpoint, params=params))
