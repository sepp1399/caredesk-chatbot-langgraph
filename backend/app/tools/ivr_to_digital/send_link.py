"""Send digital handoff link inside the IvrToDigital flow.

In a real deployment the link would be dispatched via SMS (e.g. Twilio) or
WhatsApp (e.g. Vonage). Here we cannot send a real message — no gateway is
configured — so we log the dispatch intent and surface the link to the
agent, so the agent can both:

  - tell the patient "ti ho mandato un link via SMS al numero …",
  - paste the link directly into the chat (web channel), keeping the
    handoff functional without an SMS provider.

A `cd_ivr_tag` query param is attached so conversions can be attributed
back to the IVR-to-digital flow.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.config import settings
from app.tools.shared._helpers import _log, _ok

logger = logging.getLogger("caredesk_lg.ivr2dig")

_DISPATCH_LOG = Path(__file__).resolve().parents[3] / "logs" / "ivr_dispatch.jsonl"

# Placeholder demo domain — swap for your real portal URL in production.
_LINK_TEMPLATES = {
    "registration":  "https://app.caredesk.example/{instance}/register?cd_ivr_tag=1&phone={phone}",
    "invitation":    "https://app.caredesk.example/{instance}/confirm?cd_ivr_tag=1&phone={phone}",
    "direct_booking":"https://app.caredesk.example/{instance}/book?cd_ivr_tag=1",
}


class _SendIn(BaseModel):
    phone: str = Field(description="Digits only — number to dispatch to.")
    scenario: Literal["no_user", "account_pending", "account_active"] = Field(
        description=(
            "Result of lookup_user_by_phone. "
            "Determines which link template is sent."
        ),
    )
    channel: Literal["sms", "whatsapp"] = Field(
        default="sms",
        description="Dispatch channel. Default 'sms'.",
    )


@tool(args_schema=_SendIn)
async def send_digital_link(
    phone: str,
    scenario: Literal["no_user", "account_pending", "account_active"],
    channel: Literal["sms", "whatsapp"] = "sms",
) -> str:
    """
    Send the patient a link to continue on the digital channel.

    Choose the link template from the lookup scenario:
    - no_user         → registration link
    - account_pending → invitation/confirmation link
    - account_active  → direct booking link

    The agent must read the destination phone back to the patient for
    confirmation BEFORE calling this tool.
    """
    inputs = {"phone": phone, "scenario": scenario, "channel": channel}
    norm = "".join(c for c in (phone or "") if c.isdigit())

    link_kind = {
        "no_user":         "registration",
        "account_pending": "invitation",
        "account_active":  "direct_booking",
    }[scenario]
    link = _LINK_TEMPLATES[link_kind].format(
        instance=settings.caredesk_instance_id,
        phone=norm,
    )

    record = {
        "ts":       datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "phone":    norm,
        "channel":  channel,
        "scenario": scenario,
        "link":     link,
        "instance": settings.caredesk_instance_id,
    }
    try:
        _DISPATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DISPATCH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Could not write dispatch log: %s", exc)

    logger.info("IVR→digital dispatch | %s", record)
    return _log("send_digital_link", inputs,
                _ok(channel=channel, scenario=scenario, link=link,
                    note=(
                        "Dispatch logged locally. In production the link is "
                        "sent via Twilio (SMS) or Vonage (WhatsApp); on the "
                        "web channel you may also paste the link into the chat."
                    )))
