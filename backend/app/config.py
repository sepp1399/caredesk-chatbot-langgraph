"""Fail-fast settings loader.

Reads every required environment variable at import time and raises if
anything is missing. Optional fields fall back to documented defaults.
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # Google Gemini
    gemini_api_key: str
    gemini_model: str            # main chat model used by every flow agent
    gemini_router_model: str     # smaller/faster model for the orchestrator
    # CareDesk booking backend (in-memory mock)
    caredesk_instance_id: str
    caredesk_test_user_id: str
    # When True, the booking surface exposes "weboff" activities/doctors —
    # i.e. items normally hidden from public listings — and tags them so the
    # agent can warn the patient that they can't be booked directly here.
    caredesk_manage_weboff: bool
    # Voicebot_Chatbot_Chiavi.tsv
    prompts_tsv_path: Path
    prompts_instance: str
    prompts_lang: str
    # Bot output language — controls i18n bundles (static messages,
    # quick-reply chips, KB inline reply) AND injects a language-override
    # directive into every agent's system prompt so the LLM answers in
    # the configured language regardless of the prompt's source language.
    # Supported: "it" (default) | "en".
    bot_lang: str
    # App
    log_level: str
    cors_origins: list[str]
    # Langfuse (optional) — when both keys are present, every LangChain
    # invocation is traced. Leave the keys empty to disable.
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str


def _default_tsv_path() -> Path:
    # backend/app/config.py → repo root is 3 levels up
    return Path(__file__).resolve().parents[3] / "Voicebot_Chatbot_Chiavi.tsv"


def _load() -> Settings:
    # langchain-google-genai reads GOOGLE_API_KEY by default; we accept either
    # name and forward whichever is set to the client explicitly.
    gemini_api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not gemini_api_key:
        raise RuntimeError(
            "Missing required environment variable: GEMINI_API_KEY "
            "(or GOOGLE_API_KEY)."
        )

    raw_origins = os.getenv("CORS_ORIGINS", "*")
    cors_origins = (
        ["*"] if raw_origins.strip() == "*"
        else [o.strip() for o in raw_origins.split(",") if o.strip()]
    )

    tsv_env  = os.getenv("PROMPTS_TSV_PATH", "").strip()
    tsv_path = Path(tsv_env) if tsv_env else _default_tsv_path()

    instance_id = os.getenv("CAREDESK_INSTANCE_ID", "caredesk-demo")

    return Settings(
        gemini_api_key=gemini_api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        gemini_router_model=os.getenv("GEMINI_ROUTER_MODEL", "gemini-2.5-flash-lite"),
        caredesk_instance_id=instance_id,
        caredesk_test_user_id=os.getenv("CAREDESK_TEST_USER_ID", "usr_demo_001"),
        caredesk_manage_weboff=os.getenv("CAREDESK_MANAGE_WEBOFF", "false").lower() in {"1", "true", "yes", "on"},
        prompts_tsv_path=tsv_path,
        prompts_instance=os.getenv("PROMPTS_INSTANCE", instance_id),
        prompts_lang=os.getenv("PROMPTS_LANG", "it"),
        bot_lang=os.getenv("BOT_LANG", "it").lower(),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        cors_origins=cors_origins,
        langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY", "").strip(),
        langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY", "").strip(),
        # LANGFUSE_BASE_URL is the preferred name; LANGFUSE_HOST kept for
        # back-compat with the standard Langfuse SDK env naming.
        langfuse_host=(
            os.getenv("LANGFUSE_BASE_URL")
            or os.getenv("LANGFUSE_HOST")
            or "https://cloud.langfuse.com"
        ).strip(),
    )


settings = _load()
