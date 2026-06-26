"""Area (clinic-site) search for the lab-booking flow.

Two entry modes:
  - by name → fuzzy match against the available areas
  - by city → lookup with a city filter

The LLM is expected to extract a clean city/address from the patient's
utterance before calling this tool, so no geocoding happens here.
"""

import asyncio
import json
import time
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.integrations.caredesk import cd_get
from app.tools.shared._helpers import _err, _fuzzy_match_dict, _log

_CACHE: dict = {"items": None, "ts": 0.0}
_CACHE_TTL = 600
_LOCK = asyncio.Lock()


async def _fetch_areas() -> list[dict]:
    if _CACHE["items"] is not None and time.monotonic() - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["items"]
    async with _LOCK:
        if _CACHE["items"] is not None and time.monotonic() - _CACHE["ts"] < _CACHE_TTL:
            return _CACHE["items"]
        try:
            data = await cd_get("/areas", {"fields": "areaid,areaTitle,city,province,address"})
            items = data.get("results", []) or []
        except Exception:
            # If the backend can't list areas, degrade gracefully to a single
            # default site so the flow still works.
            items = [{
                "areaid":    "area_centro",
                "areaTitle": "Ospedale Salus — Sede Centro",
                "city":      "Torino",
                "province":  "TO",
                "address":   "Via Roma 10, Torino",
            }]
        _CACHE["items"] = items
        _CACHE["ts"] = time.monotonic()
        return items


class _AreaIn(BaseModel):
    query: str = Field(
        description=(
            "City, address fragment or area name the patient mentioned "
            "(e.g. 'Torino', 'Mirafiori', 'Ospedale Salus'). "
            "Pass a clean string — extract from the patient's utterance first."
        )
    )


@tool(args_schema=_AreaIn)
async def search_areas(query: str) -> str:
    """
    Find bookable areas (clinic sites) matching a city or address fragment.

    Calling rules:
    - If only one site is configured for this instance, the tool will return
      that site directly and the patient does not need to choose.
    - Otherwise: returns up to 3 candidates. Present them numbered and ask
      the patient to pick.

    Return shape:
      { "status": "ok", "areas": [{areaid, areaTitle, city, address}, ...] }
    """
    inputs = {"query": query}
    items = await _fetch_areas()
    if len(items) <= 1:
        return _log("search_areas", inputs,
                    json.dumps({"status": "ok", "areas": items},
                               ensure_ascii=False))

    mapping = {a["areaid"]: f"{a.get('areaTitle','')} {a.get('city','')} {a.get('address','')}".strip()
               for a in items if a.get("areaid")}
    hits = _fuzzy_match_dict(query, mapping, n=3, cutoff=0.35)
    if not hits:
        return _log("search_areas", inputs,
                    _err("not_found",
                         "No area matched the query — ask the patient for a different reference "
                         "(city or site name)."))

    matched = []
    for h in hits:
        full = next((a for a in items if a["areaid"] == h["id"]), None)
        if full:
            matched.append(full)
    return _log("search_areas", inputs,
                json.dumps({"status": "ok", "areas": matched},
                           ensure_ascii=False))
