"""Per-request event emission for streaming pipeline progress over SSE.

Design
------
LangGraph nodes are plain ``async def`` functions that return a partial state
dict. To get **real-time** ``running`` → ``done`` updates (instead of both
events arriving together when the node returns), we use a ``contextvars``-
bound ``asyncio.Queue``:

    1. The FastAPI ``/search`` endpoint creates a queue and binds it for the
       lifetime of the request via :func:`bind_queue`.
    2. Nodes call :func:`emit_running` at the top and :func:`emit_done` (or
       :func:`emit_error`) before returning. These put events on the queue
       asynchronously.
    3. The endpoint drains the queue in parallel with running the graph and
       writes each event to the SSE stream.

Calls made outside a bound queue (e.g. unit tests, REPL) are silent no-ops,
so node code never has to branch on "is streaming enabled".

Wire-shape note
---------------
The dict returned by these helpers matches ``state.StepLogEntry`` exactly so
it can be appended into ``state.step_log`` without transformation. The SSE
``event:`` field (e.g. ``event: step``) is added at the FastAPI boundary —
the dict itself does not carry a ``"type": "step"`` discriminator.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator


_queue_ctx: contextvars.ContextVar["asyncio.Queue[dict] | None"] = contextvars.ContextVar(
    "agent_event_queue", default=None
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def bind_queue(queue: "asyncio.Queue[dict]") -> AsyncIterator[None]:
    """Bind ``queue`` so nodes within the ``async with`` block emit to it.

    Exiting the block resets the context — subsequent emits in the same
    process (but a different request) won't leak into this queue.
    """
    token = _queue_ctx.set(queue)
    try:
        yield
    finally:
        _queue_ctx.reset(token)


async def _put(event: dict) -> None:
    q = _queue_ctx.get()
    if q is not None:
        await q.put(event)


async def emit_running(node_id: str, text: str) -> float:
    """Emit a ``running`` step event. Returns a monotonic start marker that
    the matching :func:`emit_done` call uses to compute elapsed time."""
    await _put({
        "id": node_id,
        "text": text,
        "status": "running",
        "meta": None,
        "timestamp": _now_iso(),
        "elapsed_ms": None,
    })
    return time.monotonic()


async def emit_done(
    node_id: str,
    text: str,
    started_at: float,
    meta: str | None = None,
) -> dict:
    """Emit a ``done`` step event with elapsed timing. Returns the event dict
    so callers can also push it into ``state.step_log`` for the final result."""
    event = {
        "id": node_id,
        "text": text,
        "status": "done",
        "meta": meta,
        "timestamp": _now_iso(),
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }
    await _put(event)
    return event


async def emit_error(
    node_id: str,
    text: str,
    started_at: float | None = None,
) -> dict:
    """Emit an ``error`` step event. Used when a node raises so the UI can
    surface a red banner instead of a silent timeout."""
    event = {
        "id": node_id,
        "text": text,
        "status": "error",
        "meta": None,
        "timestamp": _now_iso(),
        "elapsed_ms": int((time.monotonic() - started_at) * 1000) if started_at else None,
    }
    await _put(event)
    return event
