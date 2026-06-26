"""Intent classifier — inbound triage for each conversational turn.

LLM-based classification with structured output (Pydantic). Falls back to
a regex heuristic if the LLM call fails.

Return shape: (intent, reasoning).
Intents: lab_booking | manage_reservations | patient_registration |
         lead_creation | ivr_to_digital | info | stay | menu.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agents.llms import small_llm
from app.config import settings
from app.observability import (
    get_langfuse_handler,
    langfuse_metadata,
    trace_session,
)

logger = logging.getLogger("caredesk_lg.orchestrator")


Intent = Literal[
    "lab_booking",
    "manage_reservations",
    "patient_registration",
    "lead_creation",
    "ivr_to_digital",
    "info",
    "stay",
    "menu",
]


class IntentDecision(BaseModel):
    intent: Intent = Field(description="Where the next turn should go.")
    reasoning: str = Field(description="One short sentence — debug only.")


_PROMPT_PATH = Path(__file__).parent / "orchestrator_prompt.md"
SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")


def reload_prompt() -> int:
    global SYSTEM_PROMPT
    SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")
    return len(SYSTEM_PROMPT)


_classifier = small_llm.with_structured_output(IntentDecision)
logger.info("Orchestrator ready — model=%s", settings.gemini_router_model)


# ── Regex fallback ───────────────────────────────────────────────────────────

# Verb patterns include both Italian and English keywords so the fast-path
# in `_fast_path` skips the LLM round-trip for either language. The
# orchestrator is shared across languages — only the patient-facing
# replies change with BOT_LANG.
_VERB_PATTERNS: list[tuple[Intent, re.Pattern]] = [
    ("manage_reservations",  re.compile(r"\b(?:disdic|disdet|disdir|cancell|spost|riprogramm|annull|elimin|cancel|reschedul|move\s+(?:my|the)\s+appoint)\w*", re.I)),
    ("ivr_to_digital",       re.compile(r"\b(?:sms|whatsapp|link\s+via|chat|digitale|digital)\w*", re.I)),
    ("lead_creation",        re.compile(r"\b(?:richiama|richiamat|operator|call.?center|contatt(?:ami|atemi)|call\s*me|callback|talk\s+to\s+(?:an?\s+)?(?:agent|human))\w*", re.I)),
    ("patient_registration", re.compile(r"\b(?:non.+registr|iscrivermi|registrarmi|nuovo.+paziente|register|sign[-\s]?up|new\s+patient)\w*", re.I)),
    ("info",                 re.compile(r"\b(?:orari|parcheggio|preparazione|prezzi?|cost(?:o|a)|convenzioni|referti|indirizzo|dove|informazion|info|hours|location|address|price|cost|insurance|report|preparation|where|when)\w*", re.I)),
    ("lab_booking",          re.compile(r"\b(?:prenot|fiss(?:a|are|ami)|book(?:ing)?|appointment|schedule|make\s+a\s+booking)\w*", re.I)),
]

_MENU_RE = re.compile(r"(?:^/menu\b)|\b(?:menu|home|inizio|iniziale|principale|ricomincia|start\s+over|restart)\w*", re.I)
_MENU_NUM_RE: dict[Intent, re.Pattern] = {
    "lab_booking":         re.compile(r"^\s*1\s*[!.]?\s*$"),
    "manage_reservations": re.compile(r"^\s*2\s*[!.]?\s*$"),
    "info":                re.compile(r"^\s*3\s*[!.]?\s*$"),
}


def _regex_classify(text: str, in_flow: bool) -> Optional[Intent]:
    if _MENU_RE.search(text):
        return "menu"
    for intent, pat in _VERB_PATTERNS:
        if pat.search(text):
            return intent
    if not in_flow:
        for intent, pat in _MENU_NUM_RE.items():
            if pat.match(text):
                return intent
    return None


# ── Public API ───────────────────────────────────────────────────────────────

def _fast_path(text: str, current_flow: Optional[str]) -> Optional[tuple[Intent, str]]:
    """Try to answer without an LLM round-trip.

    The previous implementation called the small LLM on EVERY turn — even
    when the user was deep inside a flow typing things like "sì", "il primo",
    "ok". That's a full Gemini request added serially before the main
    agent runs. We keep the LLM only for the genuinely ambiguous cases:

    - regex catches a flow verb / menu / info keyword → trust regex,
    - user is in a flow and regex is silent → "stay" (don't churn the LLM
      to confirm what the chat state already says),
    - user is NOT in a flow and regex is silent → fall through to LLM
      (intent really does need to be classified to enter a flow).
    """
    regex = _regex_classify(text, in_flow=current_flow is not None)
    if regex is not None:
        return regex, "regex fast-path"
    if current_flow is not None:
        return "stay", "in-flow + no flow-switch verb → stay"
    return None


def classify_intent(
    text: str,
    current_flow: Optional[str] = None,
    last_bot_message: Optional[str] = None,
) -> tuple[Intent, str]:
    """Synchronous classifier. Prefer `aclassify_intent` in async code so
    the event loop isn't blocked on the model round-trip."""
    shortcut = _fast_path(text, current_flow)
    if shortcut is not None:
        intent, reason = shortcut
        logger.info("intent=%s  flow=%s  text=%.80r  reason=%s",
                    intent, current_flow, text, reason)
        return intent, reason

    user_block = (
        f"current_flow: {current_flow or 'null'}\n"
        f"last_bot_message: {last_bot_message or '(none)'}\n"
        f"patient_message: {text}"
    )
    try:
        decision: IntentDecision = _classifier.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_block),
        ])
        logger.info(
            "intent=%s  flow=%s  text=%.80r  reason=%.80r",
            decision.intent, current_flow, text, decision.reasoning,
        )
        return decision.intent, decision.reasoning
    except Exception as exc:
        logger.warning("Orchestrator LLM failed (%s) — falling back to regex", exc)
        regex = _regex_classify(text, in_flow=current_flow is not None)
        if regex is None:
            return ("stay" if current_flow else "menu"), f"regex fallback ({exc})"
        return regex, f"regex fallback ({exc})"


def _classifier_config(session_id: str | None) -> dict | None:
    """Build the LangChain `config` for the classifier call.

    Returns `None` when there's nothing to attach (no session_id and no
    Langfuse handler) so callers can pass it through `ainvoke` without
    overhead.
    """
    handler = get_langfuse_handler()
    if session_id is None and handler is None:
        return None
    cfg: dict = {
        "tags": ["orchestrator", "caredesk_lg"],
        "run_name": "intent_classifier",
    }
    if session_id is not None:
        cfg["metadata"] = {
            "session_id": session_id,
            **langfuse_metadata(session_id, "orchestrator"),
        }
    if handler is not None:
        cfg["callbacks"] = [handler]
    return cfg


async def aclassify_intent(
    text: str,
    current_flow: Optional[str] = None,
    last_bot_message: Optional[str] = None,
    session_id: Optional[str] = None,
) -> tuple[Intent, str]:
    """Async version — uses `ainvoke` so the event loop isn't blocked
    while waiting on the Gemini round-trip. Falls through to the
    regex fast-path first (no LLM call when in-flow + no flow-switch verb).

    `session_id` is forwarded to the Langfuse handler so the classifier
    call appears under the same trace session as the agent turns.
    """
    shortcut = _fast_path(text, current_flow)
    if shortcut is not None:
        intent, reason = shortcut
        logger.info("intent=%s  flow=%s  text=%.80r  reason=%s",
                    intent, current_flow, text, reason)
        return intent, reason

    user_block = (
        f"current_flow: {current_flow or 'null'}\n"
        f"last_bot_message: {last_bot_message or '(none)'}\n"
        f"patient_message: {text}"
    )
    try:
        cfg = _classifier_config(session_id)
        invoke_kwargs = {"config": cfg} if cfg is not None else {}
        if session_id is not None:
            ctx = trace_session(
                name="intent_classifier",
                session_id=session_id,
                tags=["orchestrator", "caredesk_lg"],
                metadata={"current_flow": current_flow or "null"},
            )
        else:
            from contextlib import nullcontext
            ctx = nullcontext()
        with ctx:
            decision: IntentDecision = await _classifier.ainvoke(
                [SystemMessage(content=SYSTEM_PROMPT),
                 HumanMessage(content=user_block)],
                **invoke_kwargs,
            )
        logger.info(
            "intent=%s  flow=%s  text=%.80r  reason=%.80r",
            decision.intent, current_flow, text, decision.reasoning,
        )
        return decision.intent, decision.reasoning
    except Exception as exc:
        logger.warning("Orchestrator LLM failed (%s) — falling back to regex", exc)
        regex = _regex_classify(text, in_flow=current_flow is not None)
        if regex is None:
            return ("stay" if current_flow else "menu"), f"regex fallback ({exc})"
        return regex, f"regex fallback ({exc})"
