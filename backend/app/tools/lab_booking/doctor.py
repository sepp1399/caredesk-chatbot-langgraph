"""Doctor (resource) tools for the lab-booking flow."""

import asyncio
import json
import re
import time
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.config import settings
from app.integrations.caredesk import cd_get
from app.tools.shared._helpers import _err, _fuzzy_match_dict, _log, _ok

_CACHE: dict = {"items": None, "ts": 0.0}
_WEBOFF_CACHE: dict = {"ids": set(), "ts": 0.0}
_CACHE_TTL = 300
_LOCK = asyncio.Lock()

# Strip the common Italian title prefixes so the LLM fuzzy-matches against
# clean names. `DOTT.SSA` is matched FIRST so the female title is removed in
# one shot (matching `DOTT.` first would leave a dangling `SSA`).
_PREFIX_RE = re.compile(r"\bDOTT\.?\s*SSA\.?\b|\bDOTT\.?\b|\bDR\.?\b", re.IGNORECASE)


def _normalize_doctor_name(raw: str) -> str:
    if not raw:
        return ""
    name = _PREFIX_RE.sub(" ", raw)
    # Drop any punctuation the prefix left behind (e.g. the dot in "Dott. X").
    name = re.sub(r"^[.\s]+", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name.title()


async def _fetch_doctor_map() -> dict[str, str]:
    """Return {resourceid: normalized_name}.

    When `settings.caredesk_manage_weboff` is on, also fetch `isHidden`
    and populate `_WEBOFF_CACHE['ids']` with the resourceids of hidden
    doctors — those are returned in the mapping (so the LLM can match
    them) but flagged downstream as not bookable via this channel.
    """
    if _CACHE["items"] is not None and time.monotonic() - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["items"]
    async with _LOCK:
        if _CACHE["items"] is not None and time.monotonic() - _CACHE["ts"] < _CACHE_TTL:
            return _CACHE["items"]
        weboff_on = settings.caredesk_manage_weboff
        fields = "resourceid,name,isHidden" if weboff_on else "resourceid,name"
        data = await cd_get("/resources", {"fields": fields, "lang": "it"})
        items = data.get("results", []) or []
        mapping: dict[str, str] = {}
        weboff_ids: set[str] = set()
        for it in items:
            rid, raw = it.get("resourceid"), it.get("name")
            if not rid or not raw:
                continue
            clean = _normalize_doctor_name(raw)
            if not clean:
                continue
            mapping[rid] = clean
            if weboff_on:
                try:
                    is_hidden = int(it.get("isHidden", 0)) != 0
                except (TypeError, ValueError):
                    is_hidden = False
                if is_hidden:
                    weboff_ids.add(rid)
        _CACHE["items"] = mapping
        _CACHE["ts"] = time.monotonic()
        _WEBOFF_CACHE["ids"] = weboff_ids
        _WEBOFF_CACHE["ts"] = time.monotonic()
        return mapping


async def _is_doctor_weboff(resourceid: str) -> bool:
    if not settings.caredesk_manage_weboff:
        return False
    # Ensure the cache is primed.
    if _CACHE["items"] is None:
        await _fetch_doctor_map()
    return resourceid in _WEBOFF_CACHE.get("ids", set())


class _SearchDoctorsIn(BaseModel):
    picked_name: Optional[str] = Field(
        default=None,
        description=(
            "Doctor name the patient confirmed in the current turn. "
            "Pass null/omit in two scenarios:\n"
            "  • silent INIT at phase entry — return the {id: name} map "
            "    only, no commit. The pre_model_hook prefetches the tool "
            "    this way for you.\n"
            "  • explicit 'any doctor' commit — when the patient delegates "
            "    the choice ('scegli tu', 'qualunque va bene', etc.) "
            "    AND the agent intends to commit, call with "
            "    picked_name=null AND commit=true.\n"
            "Inverted word order ('Rossi Mario' = 'Mario Rossi'), "
            "typos and surname-only are all tolerated."
        ),
    )
    commit: bool = Field(
        default=False,
        description=(
            "Set true when the call is meant to COMMIT the patient's "
            "choice (not just refresh the list). Required to unlock the "
            "next phase. Combined with picked_name=null it means 'commit "
            "any doctor / no preference'."
        ),
    )


@tool(args_schema=_SearchDoctorsIn)
async def search_doctor_names(picked_name: Optional[str] = None,
                              commit: bool = False) -> str:
    """
    Doctor-phase tool. Two modes:

    1. **List mode** (default, no commit): return {resourceid: name} for
       fuzzy-matching the patient's input. Call silently at phase 2
       entry — the pre_model_hook handles this prefetch for you.
       Never expose the full list to the patient.

    2. **Commit mode** (commit=true): persist the patient's choice and
       unlock phase 3 (SERVICE).
       - Patient named a doctor → pass picked_name with that name and
         commit=true. Exact + fuzzy match (inverted order, typos,
         surname-only) auto-accepted.
       - Patient delegated ("scegli tu", "qualunque", "no preference")
         → pass picked_name=null and commit=true.
       - Ambiguous fuzzy match (≥2 candidates) → returns status='ambiguous'
         with the candidate list; the agent re-asks the patient. Do NOT
         pre-filter the suggestions yourself — present them exactly.

    GUARD on commit mode: only call when the patient named or confirmed
    a doctor IN THE CURRENT TURN. Never infer from past reservations.
    """
    inputs = {"picked_name": picked_name, "commit": commit}

    try:
        mapping = await _fetch_doctor_map()
    except Exception as exc:
        return _log("search_doctor_names", inputs, _err("backend_error", str(exc)))

    # List mode — no commit, just return the map.
    if not commit:
        return _log("search_doctor_names", inputs,
                    json.dumps(mapping, ensure_ascii=False))

    # Commit mode below.
    norm = (picked_name or "").lower().strip()

    if not norm:
        # Explicit "any doctor" commit.
        return _log("search_doctor_names", inputs, _ok(mode="any_doctor"))

    async def _maybe_weboff(rid: str, name: str) -> str | None:
        """Return a weboff error JSON if the chosen doctor isn't bookable
        via this channel, or None when the doctor is bookable."""
        if await _is_doctor_weboff(rid):
            return _err(
                "weboff",
                f"Doctor {name} cannot be booked directly through chat. "
                "Offer the patient an operator callback "
                "(transfer_to_flow('lead_creation', ...)) or to switch "
                "to the web portal.",
            )
        return None

    for rid, name in mapping.items():
        nlow = name.lower()
        if nlow == norm or " ".join(nlow.split()[::-1]) == norm:
            weboff_err = await _maybe_weboff(rid, name)
            if weboff_err:
                return _log("search_doctor_names", inputs, weboff_err)
            return _log("search_doctor_names", inputs,
                        _ok(mode="picked", doctor=name, doctor_id=rid,
                            match_type="exact"))

    suggestions = _fuzzy_match_dict(picked_name, mapping)
    if len(suggestions) == 1:
        weboff_err = await _maybe_weboff(suggestions[0]["id"], suggestions[0]["name"])
        if weboff_err:
            return _log("search_doctor_names", inputs, weboff_err)
        # Single fuzzy candidate → auto-accept (no "did you mean…?"
        # bounce). User flagged that bounce as friction.
        result = _ok(
            mode="picked",
            doctor=suggestions[0]["name"],
            doctor_id=suggestions[0]["id"],
            match_type="fuzzy",
        )
    elif len(suggestions) > 1:
        # Strip weboff entries from the ambiguous proposals — the patient
        # should only choose among bookable doctors.
        bookable = []
        for s in suggestions:
            if not await _is_doctor_weboff(s["id"]):
                bookable.append(s)
        if not bookable:
            result = _err(
                "weboff",
                "None of the matching doctors can be booked through chat. "
                "Offer an operator callback or the web portal.",
            )
        else:
            result = json.dumps(
                {"status": "ambiguous", "suggestions": bookable},
                ensure_ascii=False,
            )
    else:
        result = _err(
            "not_found",
            "Doctor not found. Ask for another name, or commit with "
            "picked_name=null + commit=true if the patient has no preference.",
        )
    return _log("search_doctor_names", inputs, result)
