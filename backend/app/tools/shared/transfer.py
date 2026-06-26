"""Cross-flow handoff tool.

Any agent may call `transfer_to_flow(target=..., reason=...)`. The router
intercepts the call, switches the flow registry, and runs the target
agent with a kickoff message in the same turn.
"""

from __future__ import annotations

import logging
from typing import Literal

from langchain_core.tools import tool

logger = logging.getLogger("caredesk_lg.transfer")


@tool
def transfer_to_flow(
    target: Literal[
        "lab_booking",
        "manage_reservations",
        "patient_registration",
        "lead_creation",
        "ivr_to_digital",
    ],
    reason: str,
) -> str:
    """
    Hand off the conversation to a different agent.

    Use when the patient's request is outside your scope, or when the
    patient explicitly accepts a follow-up that needs another flow.

    Examples:
    - LabBooking patient is unauthenticated → transfer_to_flow('patient_registration', …).
    - LabBooking has no slot match and patient wants to be called back →
      transfer_to_flow('lead_creation', …).
    - ManageReservations cannot find the appointment and patient wants to
      book a new one → transfer_to_flow('lab_booking', …).

    After calling this tool, write a one-sentence handoff message
    in the bot's configured output language.
    """
    logger.info("Transfer requested → target=%s reason=%.120r", target, reason)
    return f"Transfer queued to '{target}'. The target agent will pick up immediately."
