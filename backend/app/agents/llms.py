"""Google Gemini chat models.

Exposes one main `llm` for the ReAct flow agents and one smaller
`small_llm` for the intent classifier (or any other lightweight call
that doesn't need heavy reasoning). Both authenticate with a single
Gemini API key (`GEMINI_API_KEY`).
"""

import logging

from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import settings

logger = logging.getLogger("caredesk_lg.llms")

# Gemini streams content chunks when consumers call astream / astream_events,
# and performs parallel function calling natively — so the ReAct loop can
# batch independent tool calls (e.g. search_insurance_names +
# search_doctor_names at phase boundaries) without any extra flag.
llm = ChatGoogleGenerativeAI(
    model=settings.gemini_model,
    google_api_key=settings.gemini_api_key,
)
logger.info("Main LLM ready — model=%s", settings.gemini_model)

# Temperature 0 keeps intent classification deterministic.
small_llm = ChatGoogleGenerativeAI(
    model=settings.gemini_router_model,
    google_api_key=settings.gemini_api_key,
    temperature=0,
)
logger.info("Small LLM ready — model=%s", settings.gemini_router_model)
