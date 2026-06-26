"""Phone lookup for the IVR→digital handoff."""

from __future__ import annotations

import logging

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.integrations.caredesk import cd_get
from app.tools.shared._helpers import MIN_PHONE_DIGITS, _err, _log, _ok, normalize_digits

logger = logging.getLogger("caredesk_lg.ivr2dig.lookup")

_USER_FIELDS = "userid,memberid,name,surname,email,phone,hometel,has_account"
_TRUTHY = {True, 1, "1", "true", "True"}


class _PhoneLookupIn(BaseModel):
    phone: str = Field(description="Digits only; country prefix optional.")


@tool(args_schema=_PhoneLookupIn)
async def lookup_user_by_phone(phone: str) -> str:
    """
    Look up registered users for a phone number (mobile AND landline).

    Returns one of three scenarios that drive the IVR→digital handoff:
    - `no_user`        : phone unknown → send the registration-invitation link.
    - `account_pending`: user exists but `has_account=False` → send the
                         confirmation link (legacy 'TRD92' invitation).
    - `account_active` : at least one matching user has `has_account=True`
                         → send the direct booking link.

    The agent then calls `send_digital_link` with the right scenario.
    """
    inputs = {"phone": phone}
    norm = normalize_digits(phone)
    if len(norm) < MIN_PHONE_DIGITS:
        return _log("lookup_user_by_phone", inputs,
                    _err("invalid_phone", "Phone too short."))

    try:
        data = await cd_get("/users", {
            "profile":          "EXTERNAL_USER",
            "exclude_children": "ON",
            "phone":            norm,
            "hometel":          norm,
            "fields":           _USER_FIELDS,
        })
    except Exception as exc:
        logger.warning("lookup_user_by_phone failed: %s", exc)
        return _log("lookup_user_by_phone", inputs,
                    _err("backend_error", str(exc)))

    matches = data.get("results") or []
    if not matches:
        return _log("lookup_user_by_phone", inputs, _ok(scenario="no_user"))

    active = next((u for u in matches if u.get("has_account") in _TRUTHY), None)
    primary = active or matches[0]
    scenario = "account_active" if active else "account_pending"
    user = {
        "userid":  primary.get("userid") or primary.get("memberid"),
        "name":    primary.get("name"),
        "surname": primary.get("surname"),
    }
    return _log("lookup_user_by_phone", inputs,
                _ok(scenario=scenario, user=user))
