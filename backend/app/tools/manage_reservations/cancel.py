"""Cancel an existing reservation.

  DELETE /reservations/{resid}?reason=<source>

The `reason` query parameter is a free-text audit marker so the operations
team can tell bot-driven cancellations apart from operator ones; we tag
every cancellation from here as `VOICEBOT`.
"""

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.integrations.caredesk import cd_delete_envelope
from app.tools.shared._helpers import _err, _log, _ok

_CANCEL_REASON = "VOICEBOT"


class _CancelIn(BaseModel):
    resid: str = Field(
        description=(
            "Reservation id from list_my_reservations. The patient must "
            "have explicitly confirmed cancellation on a summary you just "
            "presented (date, time, service)."
        )
    )


@tool(args_schema=_CancelIn)
async def cancel_reservation(resid: str) -> str:
    """
    Cancel an existing reservation.

    GUARD: only call AFTER the patient has explicitly confirmed (e.g.
    "yes cancel") on a summary that includes date, time and service.
    Cancellation is irreversible.
    """
    inputs = {"resid": resid}
    try:
        envelope = await cd_delete_envelope(f"/reservations/{resid}",
                                            params={"reason": _CANCEL_REASON})
    except Exception as exc:
        return _log("cancel_reservation", inputs, _err("backend_error", str(exc)))

    return _log("cancel_reservation", inputs,
                _ok(resid=resid, cancelled=True, response=envelope.get("return")))
