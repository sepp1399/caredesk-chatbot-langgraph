"""List the patient's upcoming reservations."""

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.config import settings
from app.integrations.caredesk import cd_get
from app.tools.shared._helpers import _err, _log, _ok


class _ListIn(BaseModel):
    userid: str | None = Field(
        default=None,
        description=(
            "Authenticated patient's userid. Falls back to "
            "CAREDESK_TEST_USER_ID when null (development sessions)."
        ),
    )


async def _list_my_reservations_impl(userid: str | None = None) -> str:
    """Underlying coroutine — used both by the @tool wrapper exposed to the
    LLM and by `_synthesize_list_call` in the manage_reservations agent's
    pre_model_hook (which needs to pre-fetch the patient's reservations
    without going through the LLM)."""
    inputs = {"userid": userid}
    uid = userid or settings.caredesk_test_user_id
    try:
        # futureRes restricts to upcoming reservations; is_pending filters to
        # the actionable states (approved / to-approve / user-confirmed).
        data = await cd_get("/reservations", {
            "userids[]":    [uid],
            "futureRes":    "true",
            "is_pending[]": [0, 1, 2],
            "orders[]":     ["start_date", "startTime"],
            "orderWay":     "ASC",
            "fields":       "resid,activityid,activityTitle,resourceid,resourceName,areaid,areaTitle,start_date,startTime,endTime,insuranceid,is_cancellable,is_reschedulable",
            "pager_limit":  10,
        })
    except Exception as exc:
        return _log("list_my_reservations", inputs, _err("backend_error", str(exc)))

    items = data.get("results", []) or []
    return _log("list_my_reservations", inputs,
                _ok(reservations=items, count=len(items)))


@tool(args_schema=_ListIn)
async def list_my_reservations(userid: str | None = None) -> str:
    """
    Return the patient's upcoming reservations.

    Use silently at the start of the manage-reservations flow, BEFORE
    asking what they want to do. Each reservation carries a `resid`
    (internal — never reveal to the patient) plus human-readable fields
    for the agent to summarise.
    """
    return await _list_my_reservations_impl(userid=userid)
