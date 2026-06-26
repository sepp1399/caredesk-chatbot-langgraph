"""Booking tools for the lab-booking flow.

Two paths:

  - `book_appointment` → POST /reservations
        Immediate booking on a slot returned by search_dates.

  - `request_deferred_appointment` → POST /activities/{activityid}/_apprequest
        Used when the activity's `mopBookability == 'DEFERRED_EMAIL'`: there
        is no slot to pick, the patient just registers an intent and the
        clinic confirms later by email.

The backend envelope for both endpoints is
`{result, return, additional_return, ...}`:
  - `return` is the newly-created `resid` (string),
  - `additional_return.reservations.{resid}` carries the reservation detail
    (immediate booking only).

We call `cd_post_envelope` so the helper does NOT discard the
`additional_return` field.
"""

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.config import settings
from app.integrations.caredesk import cd_post_envelope
from app.tools.lab_booking.availability import SLOT_CACHE
from app.tools.lab_booking.service import find_activity
from app.tools.shared._helpers import LOCATION, PRICE, _err, _log, _ok


# ── Immediate booking ────────────────────────────────────────────────────────


class _BookIn(BaseModel):
    slotid: str = Field(
        description=(
            "Slot ID from search_dates / get_new_dates. Pass back exactly as "
            "received — never invent or modify."
        )
    )
    userid: str | None = Field(
        default=None,
        description=(
            "Authenticated patient's userid (from authenticate_user_by_*). "
            "Falls back to CAREDESK_TEST_USER_ID for development sessions."
        ),
    )


@tool(args_schema=_BookIn)
async def book_appointment(slotid: str, userid: str | None = None) -> str:
    """
    Create an IMMEDIATE patient reservation.

    Single point at which the booking becomes real. Call ONLY after the
    patient has EXPLICITLY confirmed a summary you presented containing
    date, time, service, doctor and price. Posts the slot fields to
    `/reservations`; the response embeds the new `resid` and the full
    reservation detail.

    For services whose `mopBookability == 'DEFERRED_EMAIL'`, use
    `request_deferred_appointment` instead — there is no slot to book.
    """
    inputs = {"slotid": slotid, "userid": userid}

    slot = SLOT_CACHE.get(slotid)
    if not slot:
        return _log("book_appointment", inputs,
                    _err("slot_not_found",
                         "slotid not in recent search results — re-run search_dates."))

    uid = userid or settings.caredesk_test_user_id
    body = {
        "userid":              uid,
        "activityid":          slot.get("activityid", ""),
        "resourceid":          slot.get("resourceid", ""),
        "areaid":              slot.get("areaid", ""),
        "insuranceid":         slot.get("insuranceid", ""),
        "start_date":          slot.get("start_date", ""),
        "end_date":            slot.get("end_date") or slot.get("start_date", ""),
        "startTime":           slot.get("startTime", ""),
        "endTime":             slot.get("endTime", ""),
        "provider_session_id": slot.get("provider_session_id", ""),
        "searchid":            slot.get("searchid", ""),
    }

    try:
        envelope = await cd_post_envelope("/reservations", body)
    except Exception as exc:
        return _log("book_appointment", inputs, _err("backend_error", str(exc)))

    resid = envelope.get("return")
    if not resid or not isinstance(resid, str):
        return _log("book_appointment", inputs,
                    _err("booking_failed", "CareDesk did not return a reservation id."))

    detail = (envelope.get("additional_return") or {}) \
        .get("reservations", {}).get(resid, {}) or {}

    booking = {
        "resid":        resid,
        "slotid":       slotid,
        "start_date":   detail.get("start_date") or slot.get("start_date"),
        "startTime":    detail.get("startTime") or slot.get("startTime"),
        "endTime":      detail.get("endTime") or slot.get("endTime"),
        "doctor_name":  detail.get("resourceName") or slot.get("doctor_name", ""),
        "location":     detail.get("areaTitle") or slot.get("areaTitle") or LOCATION,
        "price":        detail.get("price_end") or detail.get("price")
                        or slot.get("activityPrice") or PRICE["min"],
        "status":       detail.get("status_string") or detail.get("status_code"),
        "checkin_code": detail.get("checkin_code"),
        "deferred":     False,
    }
    return _log("book_appointment", inputs, _ok(booking=booking))


# ── Deferred-email booking (no slot picked) ──────────────────────────────────


class _DeferredIn(BaseModel):
    activityid: str = Field(
        description=(
            "Activity ID from search_available_services (the service whose "
            "`mopBookability == 'DEFERRED_EMAIL'`)."
        )
    )
    userid: str | None = Field(
        default=None,
        description="Authenticated userid; falls back to CAREDESK_TEST_USER_ID.",
    )
    resourceid: str | None = Field(default=None, description="Doctor ID, if chosen.")
    insuranceid: str | None = Field(default=None, description="Insurance ID, if any.")
    areaid: str | None = Field(default=None, description="Area ID, if any.")
    area_title: str | None = Field(
        default=None,
        description="Area title (human-readable). Optional but recommended.",
    )
    area_address: str | None = Field(
        default=None,
        description="Area address. Optional but recommended.",
    )


@tool(args_schema=_DeferredIn)
async def request_deferred_appointment(
    activityid: str,
    userid: str | None = None,
    resourceid: str | None = None,
    insuranceid: str | None = None,
    areaid: str | None = None,
    area_title: str | None = None,
    area_address: str | None = None,
) -> str:
    """
    Register a DEFERRED reservation REQUEST (no slot to book).

    Use this ONLY when the chosen service has `mopBookability == 'DEFERRED_EMAIL'`
    — there are no self-bookable slots for such services and the request is
    confirmed later by an operator via email. Posts to
    `/activities/{activityid}/_apprequest` with body
    `{activityid, resourceid, insuranceid, areaid, requested_for_userid,
       areaTitle?, areaAddress?}`.

    Read the privacy/contact summary back to the patient and obtain explicit
    confirmation in the current turn before calling.
    """
    inputs = {"activityid": activityid, "userid": userid, "resourceid": resourceid,
              "insuranceid": insuranceid, "areaid": areaid,
              "area_title": area_title, "area_address": area_address}

    if not activityid:
        return _log("request_deferred_appointment", inputs,
                    _err("missing_activityid",
                         "activityid is required for a deferred request."))

    uid = userid or settings.caredesk_test_user_id
    body: dict = {
        "activityid":           activityid,
        "resourceid":           resourceid or "",
        "insuranceid":          insuranceid or "",
        "areaid":               areaid or "",
        "requested_for_userid": uid,
    }
    if area_title:
        body["areaTitle"] = area_title
    if area_address:
        body["areaAddress"] = area_address

    try:
        envelope = await cd_post_envelope(f"/activities/{activityid}/_apprequest", body)
    except Exception as exc:
        return _log("request_deferred_appointment", inputs,
                    _err("backend_error", str(exc)))

    resid = envelope.get("return")
    if not resid:
        return _log("request_deferred_appointment", inputs,
                    _err("deferred_failed",
                         "CareDesk did not return a deferred request id."))

    activity = (await find_activity(activityid)) or {}
    booking = {
        "deferred_resid": resid,
        "activityid":     activityid,
        "activity":       activity.get("activity", ""),
        "typology":       activity.get("typology", ""),
        "deferred":       True,
    }
    return _log("request_deferred_appointment", inputs, _ok(booking=booking))
