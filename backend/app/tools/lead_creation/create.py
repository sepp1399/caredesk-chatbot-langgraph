"""Lead creation — callback request for cases the bot cannot close.

Posts to the backend `/leads` endpoint and always also appends the lead to
a local JSONL log (`backend/logs/leads.jsonl`) as a safety net, so no
information is lost if the endpoint is unavailable.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.integrations.caredesk import cd_post
from app.tools.shared._helpers import _err, _log, _ok

logger = logging.getLogger("caredesk_lg.lead")

_LEADS_FILE = Path(__file__).resolve().parents[3] / "logs" / "leads.jsonl"


class _LeadIn(BaseModel):
    full_name: str         = Field(description="Patient's full name as stated.")
    phone:     str         = Field(description="Phone number, digits only.")
    reason:    str         = Field(
        description=(
            "Why the lead is being created — what the patient asked for. "
            "One short sentence (e.g. 'wants pricing for orthodontics')."
        )
    )
    email:     Optional[str] = Field(default=None, description="Optional email (basic format check).")
    interest:  Optional[str]      = Field(
        default=None,
        description=(
            "Optional tag — service/specialty of interest "
            "(e.g. 'cardiology', 'orthodontics'). Free text."
        ),
    )


@tool(args_schema=_LeadIn)
async def create_lead(
    full_name: str,
    phone: str,
    reason: str,
    email: Optional[str] = None,
    interest: Optional[str] = None,
) -> str:
    """
    Create a contact lead for the call center to follow up.

    Use when the patient's intent cannot be resolved through booking,
    manage-reservations or the knowledge base — e.g. service not in the
    catalog, pricing not known, complex case needing a human operator.

    GUARD: read back name/phone (and email if given) for confirmation in
    the current turn BEFORE calling.
    """
    inputs = {"full_name": full_name, "phone": phone, "email": email,
              "reason": reason, "interest": interest}

    phone_digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(phone_digits) < 6:
        return _log("create_lead", inputs,
                    _err("invalid_phone", "Phone too short — re-ask the patient."))

    record = {
        "ts":        datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "full_name": full_name.strip(),
        "phone":     phone_digits,
        "email":     email,
        "reason":    reason.strip(),
        "interest":  interest,
    }

    # 1. Send the lead to the backend.
    try:
        data = await cd_post("/leads", {
            "name":        full_name.strip(),
            "phone":       phone_digits,
            "email":       email,
            "description": reason.strip(),
            "interest":    interest,
        })
        record["leadid"] = data.get("leadid")
        logger.info("Lead saved → %s", record["leadid"])
    except Exception as exc:
        logger.warning("Lead endpoint failed (%s) — using local JSONL fallback", exc)

    # 2. Always persist locally as a safety net.
    try:
        _LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LEADS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Could not persist lead locally: %s", exc)

    return _log("create_lead", inputs, _ok(lead=record))
