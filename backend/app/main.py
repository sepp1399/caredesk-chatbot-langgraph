"""FastAPI entry point.

Endpoints:
  GET  /health
  GET  /welcome        — initial welcome message + quick-reply chips
  POST /chat           — single conversation turn (router dispatches)
  POST /chat/stream    — SSE token-streaming variant of /chat
  POST /reset          — wipe flow registry + per-agent checkpoints
  POST /reload-prompt  — reload all agent prompts from disk
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3
_NOISY_LOGGERS = ("httpx", "httpcore", "langchain", "urllib3",
                  "google", "google_genai", "grpc")

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_DIR / "app.log",
    maxBytes=_LOG_MAX_BYTES,
    backupCount=_LOG_BACKUPS,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)
logging.basicConfig(level=logging.DEBUG, handlers=[_file_handler, _console_handler])
for _noisy in _NOISY_LOGGERS:
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("caredesk_lg")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import RemoveMessage
from pydantic import BaseModel as PydanticModel, Field

from app.agents import router as agent_router
from app.agents.router import _AGENTS
from app.agents.lab_booking          import reload_system_prompt as reload_lab_prompt
from app.agents.manage_reservations  import reload_prompt as reload_manage_prompt
from app.agents.patient_registration import reload_prompt as reload_registration_prompt
from app.agents.lead_creation        import reload_prompt as reload_lead_prompt
from app.agents.ivr_to_digital       import reload_prompt as reload_ivr_prompt
from app.agents.orchestrator         import reload_prompt as reload_orchestrator_prompt
from app.config import settings
from app.integrations import caredesk as backend_client
from app.observability import flush_langfuse, get_langfuse_handler

logging.getLogger("caredesk_lg").setLevel(settings.log_level.upper())

_SESSION_ID_PATTERN = r"^[\w\-]+$"
_SESSION_ID_MAX = 128
_MESSAGE_MAX = 2000
_PHONE_MAX = 32
_REPLY_PREVIEW = 120
_SSE_HEADERS = {
    "Cache-Control":     "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection":        "keep-alive",
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logger.info("Application starting up")
    # Eagerly init the Langfuse client so the "tracing enabled" log
    # (or the disabled warning) shows up at startup, not on the first
    # request.
    get_langfuse_handler()
    try:
        yield
    finally:
        flush_langfuse()
        await backend_client.aclose()
        logger.info("Application shut down")


app = FastAPI(title="CareDesk LangGraph Agent", version="0.1.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class ChatRequest(PydanticModel):
    session_id:   str = Field(min_length=1, max_length=_SESSION_ID_MAX, pattern=_SESSION_ID_PATTERN)
    message:      str = Field(min_length=1, max_length=_MESSAGE_MAX)
    caller_phone: str | None = Field(default=None, max_length=_PHONE_MAX)


class ResetRequest(PydanticModel):
    session_id: str = Field(min_length=1, max_length=_SESSION_ID_MAX, pattern=_SESSION_ID_PATTERN)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
async def chat(req: ChatRequest) -> dict:
    logger.info("CHAT  session=%s  phone=%s  message=%r",
                req.session_id, req.caller_phone, req.message[:_REPLY_PREVIEW])
    try:
        result = await agent_router.dispatch(
            req.session_id, req.message, caller_phone=req.caller_phone,
        )
    except Exception:
        logger.exception("Router error  session=%s", req.session_id)
        raise HTTPException(status_code=500, detail="Internal server error")

    debug = result.get("debug", {})
    logger.info(
        "REPLY session=%s  flow=%s  phase=%s  tools=%s  replies=%d  last=%r",
        req.session_id,
        debug.get("flow"),
        debug.get("phase_name"),
        [t["tool"] for t in debug.get("tool_trace", [])],
        len(result.get("replies", [])),
        (result.get("reply") or "")[:_REPLY_PREVIEW],
    )
    return {
        "session_id":    req.session_id,
        "reply":         result.get("reply", ""),
        "replies":       result.get("replies", []),
        "quick_replies": result.get("quick_replies", []),
        "debug":         debug,
    }


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE variant of /chat — streams LLM tokens as they're generated so
    the frontend can render the bot bubble incrementally."""
    logger.info("STREAM session=%s  phone=%s  message=%r",
                req.session_id, req.caller_phone, req.message[:_REPLY_PREVIEW])

    async def _generate():
        try:
            async for event in agent_router.dispatch_stream(
                req.session_id, req.message, caller_phone=req.caller_phone,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception("Stream error  session=%s", req.session_id)
            yield f"data: {json.dumps({'type': 'error', 'message': 'internal'})}\n\n"

    return StreamingResponse(
        _generate(), media_type="text/event-stream", headers=_SSE_HEADERS,
    )


@app.get("/welcome")
def welcome() -> dict:
    """Welcome message + quick-reply buttons for a fresh session."""
    reply = agent_router.menu_reply(prefix=None)
    return {
        "reply":         reply.get("reply", ""),
        "replies":       reply.get("replies", []),
        "quick_replies": reply.get("quick_replies", []),
        "debug":         reply.get("debug", {}),
    }


@app.post("/reset")
def reset(req: ResetRequest) -> dict:
    """Wipe the session's flow registry entry and per-agent checkpoints."""
    agent_router.reset_flow(req.session_id)
    for flow_name, agent in _AGENTS.items():
        cfg = {"configurable": {"thread_id": f"{req.session_id}-{flow_name}"}}
        try:
            state = agent.get_state(cfg)
            msgs = state.values.get("messages", [])
            update: dict = {}
            if msgs:
                update["messages"] = [RemoveMessage(id=m.id) for m in msgs]
            if flow_name == "lab_booking":
                update["booking"] = {}
            if flow_name == "manage_reservations":
                update["manage"] = {}
            if update:
                agent.update_state(cfg, update)
        except Exception as exc:
            logger.warning("reset %s/%s failed: %s", req.session_id, flow_name, exc)
    logger.info("RESET session=%s", req.session_id)
    return {"status": "ok", "session_id": req.session_id}


@app.post("/reload-prompt")
def reload_prompt() -> dict:
    sizes = {
        "lab_booking":          reload_lab_prompt(),
        "manage_reservations":  reload_manage_prompt(),
        "patient_registration": reload_registration_prompt(),
        "lead_creation":        reload_lead_prompt(),
        "ivr_to_digital":       reload_ivr_prompt(),
        "orchestrator":         reload_orchestrator_prompt(),
    }
    logger.info("Prompts reloaded — %s", sizes)
    return {"status": "ok", "bytes": sizes}


_FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    logger.info("Frontend mounted at / from %s", _FRONTEND_DIR)
else:
    logger.warning("Frontend directory not found: %s", _FRONTEND_DIR)
