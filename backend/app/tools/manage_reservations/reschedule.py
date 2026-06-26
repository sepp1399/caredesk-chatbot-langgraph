"""Reschedule an existing reservation.

  PUT /reservations/{resid}
  body = { start_date, end_date, startTime, endTime,
           provider_session_id, searchid, resourceid }

This is a single atomic move: no cancel-then-rebook cascade, so there is
no window in which the patient loses the original reservation without
acquiring the new one.
"""

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.integrations.caredesk import cd_put_envelope
from app.tools.lab_booking.availability import SLOT_CACHE
from app.tools.shared._helpers import _err, _log, _ok


class _RescheduleIn(BaseModel):
    resid: str = Field(description="Existing reservation to move.")
    new_slotid: str = Field(
        description=(
            "New slot id from search_dates / get_new_dates — must be in the "
            "in-process slot cache. The patient must have explicitly "
            "confirmed this slot on a summary you presented."
        )
    )


@tool(args_schema=_RescheduleIn)
async def reschedule_reservation(resid: str, new_slotid: str) -> str:
    """
    Move an existing reservation to a new slot in a single PUT call.

    GUARD: only call AFTER the patient has explicitly confirmed the move
    on a combined summary (old appointment + new slot).
    """
    inputs = {"resid": resid, "new_slotid": new_slotid}

    slot = SLOT_CACHE.get(new_slotid)
    if not slot:
        return _log("reschedule_reservation", inputs,
                    _err("slot_not_found",
                         "new_slotid not in recent search results — re-run search_dates."))

    body = {
        "start_date":          slot.get("start_date", ""),
        "end_date":            slot.get("end_date") or slot.get("start_date", ""),
        "startTime":           slot.get("startTime", ""),
        "endTime":             slot.get("endTime", ""),
        "provider_session_id": slot.get("provider_session_id", ""),
        "searchid":            slot.get("searchid", ""),
        "resourceid":          slot.get("resourceid", ""),
    }

    try:
        envelope = await cd_put_envelope(f"/reservations/{resid}", body)
    except Exception as exc:
        return _log("reschedule_reservation", inputs, _err("backend_error", str(exc)))

    detail = (envelope.get("additional_return") or {}) \
        .get("reservations", {}).get(resid, {}) or {}

    return _log("reschedule_reservation", inputs,
                _ok(old_resid=resid,
                    new_booking={
                        "resid":       resid,  # PUT keeps the same resid
                        "start_date":  detail.get("start_date") or slot.get("start_date"),
                        "startTime":   detail.get("startTime") or slot.get("startTime"),
                        "endTime":     detail.get("endTime") or slot.get("endTime"),
                        "doctor_name": detail.get("resourceName") or slot.get("doctor_name", ""),
                    }))
