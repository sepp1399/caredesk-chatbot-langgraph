"""Optional Langfuse callback for LangChain / LangGraph traces.

When `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both set, the
shared `CallbackHandler` returned by `get_langfuse_handler()` is
attached to every agent invocation (router) and to the intent classifier
(orchestrator). When either key is missing — or the `langfuse` package
isn't importable — the helpers downgrade to no-ops and the rest of the
system runs unchanged.

This module targets the Langfuse v3 SDK. v3 routes traces through
OpenTelemetry, which changes how session correlation works for LangGraph
ReAct agents:

  - Passing `metadata={"langfuse_session_id": ...}` in the LangChain
    `config` only attaches `session_id` to the CHILD spans, not to the
    parent trace, so the UI's Sessions view stays empty.
  - The fix recommended by the Langfuse team is to open an explicit
    "enclosing span" with `langfuse.start_as_current_span(...)` and call
    `span.update_trace(session_id=..., user_id=..., tags=...)` so the
    fields land on the parent trace.

`trace_session()` below provides exactly that as a context manager and
falls back to `nullcontext()` when tracing is disabled, so callers can
use it unconditionally.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager, nullcontext
from typing import Any, ContextManager, Iterator

from app.config import settings

logger = logging.getLogger("caredesk_lg.observability")

_initialized: bool = False
_client: Any | None = None
_handler: Any | None = None


def _init() -> None:
    """Build the Langfuse client + LangChain handler once per process."""
    global _initialized, _client, _handler
    _initialized = True

    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return

    try:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:
        logger.warning(
            "langfuse import failed (%s) — tracing disabled. "
            "Install with: pip install 'langfuse>=3.0.0,<4'.",
            exc,
        )
        return

    try:
        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_host,
            # Opt into the v4 ingestion pipeline so traces show up in the
            # unified tracing table immediately. Without this header the
            # OTLP ingest falls back to the legacy path with multi-minute
            # delay and the public /api/public/traces endpoint cannot
            # find the spans yet.
            additional_headers={"x-langfuse-ingestion-version": "4"},
        )
    except Exception as exc:
        logger.warning("Langfuse client init failed (%s) — tracing disabled", exc)
        return

    try:
        ok = client.auth_check()
    except Exception as exc:
        logger.warning(
            "Langfuse auth_check raised (%s) — tracing disabled. "
            "Check LANGFUSE_BASE_URL/HOST and network reachability.",
            exc,
        )
        return
    if not ok:
        logger.warning(
            "Langfuse auth_check returned False — keys rejected by %s. "
            "Tracing disabled.",
            settings.langfuse_host,
        )
        return

    try:
        handler = CallbackHandler()
    except Exception as exc:
        logger.warning("Langfuse CallbackHandler init failed (%s) — tracing disabled", exc)
        return

    _client = client
    _handler = handler
    logger.info("Langfuse tracing enabled (host=%s)", settings.langfuse_host)


def get_langfuse_handler() -> Any | None:
    """Return a process-wide Langfuse CallbackHandler, or `None` when
    tracing is not configured.
    """
    if not _initialized:
        _init()
    return _handler


def flush_langfuse() -> None:
    """Drain pending traces. Call on shutdown so the last batch is not
    lost when the worker exits.
    """
    if _client is None:
        return
    try:
        _client.flush()
        logger.info("Langfuse queue flushed")
    except Exception as exc:
        logger.warning("Langfuse flush failed: %s", exc)


@contextmanager
def trace_session(
    *,
    name: str,
    session_id: str,
    user_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open a Langfuse parent span and attach `session_id` / `user_id` /
    `tags` to its trace.

    Yields the underlying span when tracing is enabled, or `None` when
    it is disabled, so callers can use this context unconditionally:

        with trace_session(name="lab_booking_turn", session_id=sid):
            await agent.ainvoke(state, config=cfg)

    The wrapped invocation inherits the open span via OpenTelemetry
    context propagation, which is how LangGraph child spans pick up the
    parent trace and how the Sessions view in the UI groups them.
    """
    if not _initialized:
        _init()
    if _client is None:
        yield None
        return

    span = _client.start_as_current_span(name=name)
    try:
        with span as opened:
            try:
                opened.update_trace(
                    session_id=session_id,
                    user_id=user_id,
                    tags=tags,
                    metadata=metadata,
                )
            except Exception as exc:
                logger.debug("Langfuse update_trace failed: %s", exc)
            yield opened
    except Exception as exc:
        logger.debug("Langfuse trace_session error: %s", exc)
        yield None


def langfuse_metadata(session_id: str, flow: str | None,
                      user_id: str | None = None) -> dict[str, str]:
    """Build the metadata keys the Langfuse handler recognises on child
    spans. The parent-trace fields are set by `trace_session`; these
    keys complement it so individual LLM / tool spans also carry the
    session correlation.
    """
    meta: dict[str, str] = {"langfuse_session_id": session_id}
    if flow:
        meta["flow"] = flow
    if user_id:
        meta["langfuse_user_id"] = user_id
    return meta
