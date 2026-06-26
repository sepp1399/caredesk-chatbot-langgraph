"""Shared tool helpers — response wrappers, fuzzy match, slot mapper."""

from __future__ import annotations

import json
import logging
import re
from difflib import get_close_matches
from typing import Optional

logger = logging.getLogger("caredesk_lg.tools")

# Fallback constants used only when the backend omits a field on a booking.
MAX_SLOTS: int = 6
LOCATION: str = "Ospedale Salus — Sede Centro, Via Roma 10, Torino"
PRICE: dict[str, str] = {"min": "0.00€", "max": "0.00€"}
MIN_PHONE_DIGITS: int = 6
MIN_PHONE_DIGITS_REGISTRATION: int = 8
DATE_FORMAT: str = "%d/%m/%Y"
DATETIME_FORMAT: str = "%Y-%m-%dT%H:%M:%S"


def _ok(**kw) -> str:
    return json.dumps({"status": "ok", **kw}, ensure_ascii=False)


def _err(code: str, message: str) -> str:
    return json.dumps(
        {"status": "error", "code": code, "message": message},
        ensure_ascii=False,
    )


def _log(tool_name: str, inputs: dict, result: str) -> str:
    if not logger.isEnabledFor(logging.DEBUG):
        return result
    try:
        parsed = json.loads(result)
        display = json.dumps(parsed, ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        display = result
    logger.debug(
        "[TOOL] %s\n  IN  -> %s\n  OUT -> %s",
        tool_name,
        json.dumps(inputs, ensure_ascii=False, default=str),
        display,
    )
    return result


def _map_slot(item: dict) -> dict:
    av = item.get("availability") if isinstance(item.get("availability"), dict) else item
    return {
        "slotid":              str(av.get("slotid") or av.get("provider_session_id") or ""),
        "start_date":          av.get("start_date", ""),
        "end_date":            av.get("end_date", "") or av.get("start_date", ""),
        "startTime":           av.get("startTime", ""),
        "endTime":             av.get("endTime", ""),
        "doctor_name":         av.get("resourceName", ""),
        "resourceid":          av.get("resourceid", ""),
        "activityid":          av.get("activityid", ""),
        "activityTitle":       av.get("activityTitle", ""),
        "activityPrice":       av.get("activityPrice", ""),
        "areaid":              av.get("areaid", ""),
        "areaTitle":           av.get("areaTitle", ""),
        "address":             av.get("address", ""),
        "city":                av.get("city", ""),
        "province":            av.get("province", ""),
        "insuranceid":         av.get("insuranceid", ""),
        "provider_session_id": av.get("provider_session_id", ""),
        "searchid":            av.get("searchid", ""),
    }


def _fuzzy_match_dict(name: str, mapping: dict[str, str], n: int = 3,
                      cutoff: float = 0.45) -> list[dict]:
    if not name or not mapping:
        return []
    keys, labels = list(mapping.keys()), list(mapping.values())
    lowers = [l.lower() for l in labels]
    hits = get_close_matches(name.lower(), lowers, n=n, cutoff=cutoff)
    seen: set[str] = set()
    out: list[dict] = []
    for h in hits:
        idx = lowers.index(h)
        key = keys[idx]
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": key, "name": labels[idx]})
    return out


def _fuzzy_match_names(name: str, names: list[str], n: int = 3,
                       cutoff: float = 0.5) -> list[str]:
    if not name or not names:
        return []
    rev_map = {" ".join(d.split()[::-1]).lower(): d for d in names}
    pool    = {d.lower(): d for d in names} | rev_map
    hits    = get_close_matches(name.lower(), pool.keys(), n=n, cutoff=cutoff)
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        canonical = pool[h]
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


_TIME_RANGE_RE = re.compile(r"^\s*(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s*$")


def _parse_time_range(tr: str) -> Optional[tuple[str, str]]:
    if not tr:
        return None
    m = _TIME_RANGE_RE.match(tr)
    return (m.group(1), m.group(2)) if m else None


def normalize_digits(value: str | None) -> str:
    if not value:
        return ""
    return "".join(c for c in value if c.isdigit())


_CF_RE = re.compile(r"^[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]$")


def is_valid_codice_fiscale(cf: str | None) -> bool:
    if not cf:
        return False
    return bool(_CF_RE.match(cf.upper().strip()))
