"""Insurance tools for the lab-booking flow.

Silently fetches the accepted insurances, fuzzy-matches the patient's input
against them, and special-cases 'privato' (self-pay) and SSN (which
restricts booking to laboratory tests only).
"""

import asyncio
import json
import time

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.integrations.caredesk import cd_get
from app.tools.shared._helpers import _err, _fuzzy_match_dict, _log, _ok

_PRIVATE_SENTINELS = {"privato", "privati", "privatamente", "private", "self-pay"}

# Only insurances whose mopBookability is one of these values are
# self-bookable; everything else is filtered out of the offered list.
_BOOKABLE_MOP = {"ON", "DEFERRED_EMAIL"}

# In-memory TTL cache — same shape as doctor.py / service.py. Without this
# the pre_model_hook in lab_booking re-hits /insurances on every fresh
# session entering phase 1 (the synthetic INIT call is per-session, but
# the result is generally identical across sessions).
_CACHE: dict = {"items": None, "ts": 0.0}
_CACHE_TTL = 300
_LOCK = asyncio.Lock()


async def _fetch_insurance_map() -> dict[str, str]:
    """Return {insuranceid: insurance_title} for BOOKABLE insurances only."""
    if _CACHE["items"] is not None and time.monotonic() - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["items"]
    async with _LOCK:
        # Double-checked: another coroutine may have populated the cache
        # while we were waiting for the lock.
        if _CACHE["items"] is not None and time.monotonic() - _CACHE["ts"] < _CACHE_TTL:
            return _CACHE["items"]
        data = await cd_get("/insurances", {
            "fields": "insuranceid,insurance_title,mopBookability",
        })
        items = data.get("results", []) or []
        mapping = {
            it.get("insuranceid", ""): it.get("insurance_title", "")
            for it in items
            if it.get("insuranceid") and it.get("insurance_title")
            and str(it.get("mopBookability", "")).upper() in _BOOKABLE_MOP
        }
        _CACHE["items"] = mapping
        _CACHE["ts"] = time.monotonic()
        return mapping


@tool
async def search_insurance_names() -> str:
    """
    Retrieve accepted insurance providers as {id: name}.

    Call silently at the start of the insurance phase, BEFORE asking the
    patient. Use the list to fuzzy-match input. Never expose the full list.
    """
    try:
        mapping = await _fetch_insurance_map()
        result = json.dumps(mapping, ensure_ascii=False)
    except Exception as exc:
        result = _err("backend_error", str(exc))
    return _log("search_insurance_names", {}, result)


class _InsuranceIn(BaseModel):
    insurance_name: str = Field(
        description=(
            "Insurance name confirmed by the patient. Use the exact label "
            "from search_insurance_names. For self-pay/private bookings "
            "pass the literal string 'privato'."
        )
    )


@tool(args_schema=_InsuranceIn)
async def get_insurance_id_by_insurance_name(insurance_name: str) -> str:
    """
    Persist the patient's insurance choice and unlock the next phase.

    Calling rules:
    - Self-pay → call with insurance_name='privato'.
    - Patient names an insurance → call with the exact label.
    - Ambiguous input → DO NOT call; ask the patient to disambiguate.

    On SSN: is_ssn=true means only lab tests are bookable — the agent must
    warn the patient and wait for explicit confirmation.
    """
    norm = insurance_name.lower().strip()

    if norm in _PRIVATE_SENTINELS:
        return _log("get_insurance_id_by_insurance_name",
                    {"insurance_name": insurance_name},
                    _ok(mode="private", name="PRIVATO"))

    try:
        mapping = await _fetch_insurance_map()
    except Exception as exc:
        return _log("get_insurance_id_by_insurance_name", {"insurance_name": insurance_name},
                    _err("backend_error", str(exc)))

    exact = next((k for k, v in mapping.items() if v.lower() == norm), None)
    if exact:
        is_ssn = mapping[exact].lower() == "ssn"
        result = _ok(
            insurance_id=exact,
            name=mapping[exact],
            is_ssn=is_ssn,
            ssn_warning=(
                "SSN chosen: only laboratory tests are bookable. "
                "Tell the patient and ask explicit confirmation before proceeding."
            ) if is_ssn else None,
        )
    else:
        suggestions = _fuzzy_match_dict(insurance_name, mapping)
        if suggestions:
            result = json.dumps(
                {"status": "ambiguous", "suggestions": suggestions},
                ensure_ascii=False,
            )
        else:
            result = _err(
                "not_found",
                "Insurance not in the accepted list. Ask the patient for "
                "another name or whether to proceed privately.",
            )
    return _log("get_insurance_id_by_insurance_name",
                {"insurance_name": insurance_name}, result)
