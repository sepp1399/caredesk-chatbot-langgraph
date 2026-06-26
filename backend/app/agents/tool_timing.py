"""Per-turn callback that records each tool's execution duration.

The agent traces we build for the frontend pair AIMessage tool_calls
with their ToolMessage results, but neither message carries timing.
This handler subscribes to LangChain's `on_tool_start` / `on_tool_end`
events and records elapsed milliseconds per invocation. The trace
builder then merges the timings back into the trace entries by
(tool_name, occurrence index), so the frontend can show "/<service>:
1.2 s" next to each tool card.

Lifecycle: one fresh handler per turn (instantiate inside the
router's `_dispatch_to_flow` / `_stream_flow`, pass via
`config={"callbacks": [handler]}` to the agent run, then call
`merge_timings(trace, handler.timings)` after the run completes).

Matching by (name, occurrence): tool_call_id is not reliably propagated
through the callback API in all LangGraph versions, so we fall back to
positional matching within the same turn. The agent emits the
tool_calls roughly in order; even with `parallel_tool_calls=True`,
each parallel batch has a deterministic name → call ordering inside
the AIMessage.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler


class ToolTimingHandler(AsyncCallbackHandler):
    def __init__(self) -> None:
        self._starts: dict[UUID, tuple[str, float]] = {}
        # Ordered list of (tool_name, duration_ms) in completion order.
        self.timings: list[tuple[str, int]] = []

    @staticmethod
    def _extract_name(serialized: Any, kwargs: dict) -> str:
        # Across LangChain versions the tool name turns up under different
        # keys. Check the common ones in order.
        if isinstance(serialized, dict):
            for key in ("name", "id"):
                v = serialized.get(key)
                if isinstance(v, str) and v:
                    return v
            # `id` is sometimes a path like ["langchain", "tools", "X"]
            v = serialized.get("id")
            if isinstance(v, (list, tuple)) and v:
                return str(v[-1])
        n = kwargs.get("name")
        return n if isinstance(n, str) else ""

    async def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        name = self._extract_name(serialized, kwargs)
        self._starts[run_id] = (name, time.monotonic())

    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        rec = self._starts.pop(run_id, None)
        if rec is None:
            return
        name, t0 = rec
        self.timings.append((name, int((time.monotonic() - t0) * 1000)))

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        # Still capture duration so the frontend can show how long the
        # error took to surface.
        rec = self._starts.pop(run_id, None)
        if rec is None:
            return
        name, t0 = rec
        self.timings.append((name, int((time.monotonic() - t0) * 1000)))


def merge_timings(trace: list[dict], timings: list[tuple[str, int]]) -> None:
    """Mutate the trace in place, attaching `duration_ms` to each entry.

    Pairs by (tool_name, occurrence index): the Nth call to tool X in
    the trace gets the Nth recorded duration for X. Entries with no
    matching timing keep `duration_ms` absent — the frontend just hides
    the badge.
    """
    by_name: dict[str, list[int]] = defaultdict(list)
    for name, dur in timings:
        by_name[name].append(dur)

    counter: dict[str, int] = defaultdict(int)
    for entry in trace:
        name = entry.get("tool") or ""
        idx = counter[name]
        if idx < len(by_name[name]):
            entry["duration_ms"] = by_name[name][idx]
            counter[name] += 1
