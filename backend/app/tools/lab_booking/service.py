"""Medical-service (activity) tools for the lab-booking flow."""

import asyncio
import json
import time

from langchain_core.tools import tool

from app.config import settings
from app.integrations.caredesk import cd_get
from app.tools.shared._helpers import _err, _log

_CACHE: dict = {"items": None, "ts": 0.0}
_CACHE_TTL = 300
_LOCK = asyncio.Lock()


def _is_bookable(it: dict) -> bool:
    """An activity is offered when it is not hidden, or when its booking is
    deferred-email (which is requested rather than slot-booked)."""
    try:
        not_hidden = int(it.get("isHidden", 0)) == 0
    except (TypeError, ValueError):
        not_hidden = False
    return not_hidden or str(it.get("mopBookability", "")).upper() == "DEFERRED_EMAIL"


async def _fetch_activities() -> list[dict]:
    """Fetch (and cache) activities for the current instance.

    Behaviour depends on `settings.caredesk_manage_weboff`:
      - OFF (default): hidden non-deferred items are filtered out.
      - ON: ALL activities are returned, with `weboff: true` on the ones
        that wouldn't pass `_is_bookable` (= hidden + not deferred-email),
        so the agent can warn the patient when one is mentioned.
    """
    if _CACHE["items"] is not None and time.monotonic() - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["items"]
    async with _LOCK:
        if _CACHE["items"] is not None and time.monotonic() - _CACHE["ts"] < _CACHE_TTL:
            return _CACHE["items"]
        data = await cd_get("/activities", {
            "fields": "activityid,activityTitle,typologyid,typologyTitle,mopBookability,isHidden",
        })
        raw = data.get("results", []) or []
        weboff_on = settings.caredesk_manage_weboff
        items: list[dict] = []
        for it in raw:
            if not it.get("activityid") or not it.get("activityTitle"):
                continue
            bookable = _is_bookable(it)
            if not bookable and not weboff_on:
                # Default branch: hide non-bookable entries entirely.
                continue
            entry = {
                "activityid":     it["activityid"],
                "typology":       it.get("typologyTitle", ""),
                "activity":       it["activityTitle"],
                "mopBookability": it.get("mopBookability", ""),
            }
            if weboff_on and not bookable:
                entry["weboff"] = True
            items.append(entry)
        _CACHE["items"] = items
        _CACHE["ts"] = time.monotonic()
        return items


async def is_activity_weboff(activityid: str) -> bool:
    a = await find_activity(activityid)
    return bool(a and a.get("weboff"))


async def find_activity(activityid: str) -> dict | None:
    return next(
        (a for a in await _fetch_activities() if a["activityid"] == activityid),
        None,
    )


@tool
async def search_available_services() -> str:
    """
    Retrieve medical services.

    Call silently at the start of the service phase. Present at most 3
    services at a time, numbered. When the patient picks one, proceed to
    search_dates.

    Each item carries:
      - `mopBookability` — when value is `DEFERRED_EMAIL`, booking is
        deferred (request only, confirmed later by email).
      - `weboff` (optional, only when weboff mode is on) — the service
        exists in the catalog but is NOT bookable via this channel. Don't
        propose weboff items; if the patient explicitly asks about one,
        explain that it requires the call center or the web portal.
    """
    try:
        items = await _fetch_activities()
        result = json.dumps(items, ensure_ascii=False)
    except Exception as exc:
        result = _err("backend_error", str(exc))
    return _log("search_available_services", {}, result)
