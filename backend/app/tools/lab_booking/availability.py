"""Availability tools for the lab-booking flow.

`search_dates` fetches the first available slots for a chosen service;
`get_new_dates` re-queries with a date and/or time-window filter. We pass
`bypass_availabilities_fallback=1` so the backend returns an empty list
instead of a server-side fallback, letting the agent ask the patient to
relax constraints when nothing matches.
"""

import json
from datetime import datetime
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.integrations.caredesk import cd_get
from app.tools.shared._helpers import MAX_SLOTS, _err, _log, _map_slot, _parse_time_range
from app.tools.lab_booking.service import find_activity, is_activity_weboff

_AVAILABILITY_FIELDS = ",".join([
    "slotid", "start_date", "startTime", "end_date", "endTime",
    "start_datetime_timestamp", "resourceid", "resourceName",
    "areaid", "areaTitle", "city", "province", "address",
    "activityid", "activityTitle", "activityPrice",
    "typologyid", "typologyTitle",
    "insuranceid", "insurance_title",
    "provider_session_id", "searchid",
])


def _extract_slots(data: dict) -> list[dict]:
    results = data.get("results") or {}
    items = results.get("availabilities") if isinstance(results, dict) else results
    if items is None:
        items = []
    return [_map_slot(it) for it in items]


# Slot cache populated by search_dates / get_new_dates; consumed by booking.
SLOT_CACHE: dict[str, dict] = {}


def _remember_slots(slots: list[dict]) -> None:
    for s in slots:
        sid = s.get("slotid")
        if sid:
            SLOT_CACHE[sid] = s


class _SearchDatesIn(BaseModel):
    activityid: str = Field(description="Activity ID from search_available_services.")
    insuranceid: Optional[str] = Field(
        default=None,
        description="Insurance ID. OMIT when null (private booking). Never pass the name.",
    )
    resourceid: Optional[str] = Field(
        default=None,
        description="Doctor resource ID. OMIT when any_doctor=true.",
    )
    areaid: Optional[str] = Field(
        default=None,
        description="Area ID from search_areas (multi-site instances only). OMIT for single-site.",
    )


@tool(args_schema=_SearchDatesIn)
async def search_dates(
    activityid: str,
    insuranceid: Optional[str] = None,
    resourceid: Optional[str] = None,
    areaid: Optional[str] = None,
) -> str:
    """
    Fetch first available appointment slots for the chosen service.

    Call silently right after the patient picks a service. Show AT MOST 2
    slots at a time. Do not mention price here — include it only in the
    final confirmation summary.

    Time mapping: 'morning' → '08:00-13:00' | 'afternoon' → '13:00-21:00'.
    For different dates/time windows use get_new_dates instead.
    """
    inputs = {"activityid": activityid, "insuranceid": insuranceid,
              "resourceid": resourceid, "areaid": areaid}
    if await is_activity_weboff(activityid):
        return _log("search_dates", inputs,
                    _err("weboff",
                         "This service cannot be booked through chat. "
                         "Offer the patient an operator callback (transfer_to_flow("
                         "'lead_creation', ...)) or the web portal."))
    try:
        data = await cd_get("/availabilities", {
            "activityid":                     activityid,
            "insuranceid":                    insuranceid,
            "resourceid":                     resourceid,
            "areaid":                         areaid,
            "mergerType":                     "D",
            "maxResults":                     MAX_SLOTS,
            "bypass_availabilities_fallback": 1,
            "fields":                         _AVAILABILITY_FIELDS,
        })
    except Exception as exc:
        return _log("search_dates", inputs, _err("backend_error", str(exc)))

    slots = _extract_slots(data)
    _remember_slots(slots)
    payload = {
        "status":          "ok",
        "service":         await find_activity(activityid),
        "total_available": len(slots),
        "slots":           slots,
    }
    return _log("search_dates", inputs, json.dumps(payload, ensure_ascii=False))


class _GetNewDatesIn(BaseModel):
    activityid:  str            = Field(description="Activity ID — same as search_dates.")
    insuranceid: Optional[str]  = Field(default=None, description="Insurance ID. OMIT for private.")
    resourceid:  Optional[str]  = Field(default=None, description="Doctor ID. OMIT when any_doctor=true.")
    areaid:      Optional[str]  = Field(default=None, description="Area ID for multi-site instances.")
    start_date:  Optional[str]  = Field(
        default=None,
        description=(
            "Earliest date DD/MM/YYYY. Patient gives no date → day after the "
            "last slot shown; only a month → 1st of that month; specific date → use it."
        ),
    )
    time_range:  Optional[str]  = Field(
        default=None,
        description=(
            "Time window 'HH:MM-HH:MM'. Map 'morning' → '08:00-13:00', "
            "'afternoon' → '13:00-21:00'. Null if no time preference."
        ),
    )


@tool(args_schema=_GetNewDatesIn)
async def get_new_dates(
    activityid: str,
    insuranceid: Optional[str] = None,
    resourceid: Optional[str] = None,
    areaid: Optional[str] = None,
    start_date: Optional[str] = None,
    time_range: Optional[str] = None,
) -> str:
    """
    Fetch slots filtered by date / time window.

    Call when the patient asks for a date after the last shown slot, or
    specifies morning / afternoon / explicit time. If slots=[], ask the
    patient to relax constraints (different date, or first available).
    """
    inputs = {"activityid": activityid, "insuranceid": insuranceid,
              "resourceid": resourceid, "areaid": areaid,
              "start_date": start_date, "time_range": time_range}

    if await is_activity_weboff(activityid):
        return _log("get_new_dates", inputs,
                    _err("weboff",
                         "This service cannot be booked through chat. "
                         "Offer the patient an operator callback or the web portal."))

    if start_date:
        try:
            datetime.strptime(start_date, "%d/%m/%Y")
        except ValueError:
            return _log("get_new_dates", inputs, _err("invalid_date_format", "Use DD/MM/YYYY."))

    time_bounds: Optional[tuple[str, str]] = None
    start_time_param: Optional[str] = None
    if time_range:
        time_bounds = _parse_time_range(time_range)
        if not time_bounds:
            return _log("get_new_dates", inputs, _err("invalid_time_range", "Use HH:MM-HH:MM."))
        start_time_param = time_bounds[0]

    try:
        data = await cd_get("/availabilities", {
            "activityid":                     activityid,
            "insuranceid":                    insuranceid,
            "resourceid":                     resourceid,
            "areaid":                         areaid,
            "start_date":                     start_date,
            "startTime":                      start_time_param,
            "mergerType":                     "D",
            "maxResults":                     MAX_SLOTS,
            "bypass_availabilities_fallback": 1,
            "fields":                         _AVAILABILITY_FIELDS,
        })
    except Exception as exc:
        return _log("get_new_dates", inputs, _err("backend_error", str(exc)))

    slots = _extract_slots(data)
    if time_bounds:
        t0, t1 = time_bounds
        slots = [s for s in slots if t0 <= s.get("startTime", "") <= t1]

    _remember_slots(slots)
    payload = {
        "status":          "ok",
        "service":         await find_activity(activityid),
        "total_available": len(slots),
        "slots":           slots,
    }
    return _log("get_new_dates", inputs, json.dumps(payload, ensure_ascii=False))
