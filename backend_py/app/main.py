"""FastAPI entry point — exposes the LangGraph pipeline over Server-Sent Events.

Endpoints
---------
* ``POST /search`` — streaming pipeline run. Body: ``{query, location?}``.
  Response: ``text/event-stream`` with three event kinds matching the
  existing JS frontend's ``App.jsx`` consumer byte-for-byte:

    event: step\\n
    data: {"id":"plan","text":"...","status":"running","meta":null,"elapsed":null}\\n
    \\n
    event: result\\n
    data: {"query":"...","schema":{...},"entities":[...],"sources":[...],"elapsed":12345}\\n
    \\n
    event: error\\n
    data: {"message":"..."}\\n
    \\n

* ``GET /health`` — liveness + key-presence probe (no secret values leak).

Concurrency
-----------
Each request gets its own ``asyncio.Queue`` bound via the
``contextvars``-based ``bind_queue`` context manager (see
``streaming/events.py``). Because contextvars are snapshotted per-task,
two concurrent ``/search`` requests have isolated event streams — no
risk of cross-wiring even though all nodes call the same global
``emit_*`` functions.

Why ``astream`` instead of ``ainvoke``
--------------------------------------
Both would work since events flow through the contextvar queue rather
than the graph's return value, but ``astream`` provides one extra yield
point per node, giving the asyncio scheduler more opportunities to flip
to the SSE-streamer task. With ``ainvoke`` the entire graph runs as
one coroutine and the consumer only drains the queue between node
``await`` points — slightly less responsive UI under load.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .config import get_settings
from .graph import build_graph
from .graph.state import initial_state
from .streaming.events import bind_queue


logger = logging.getLogger(__name__)

# ─── App + middleware ──────────────────────────────────────────────────────


app = FastAPI(
    title="Agentic Search Backend",
    description="LangGraph + MCP pipeline that turns a topic query into a structured entity table.",
    version="1.0.0",
)

# Configurable via ALLOWED_ORIGINS env var (comma-separated). Defaults to '*'
# so local dev and quick Railway/Render deploys just work. Tighten in prod
# by setting e.g. ALLOWED_ORIGINS="https://your-frontend.vercel.app".
_origins_env = os.environ.get("ALLOWED_ORIGINS", "*").strip()
_allowed_origins = ["*"] if _origins_env == "*" else [o.strip() for o in _origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    # SSE doesn't need credentials; keeping this False lets us use allow_origins=["*"].
    allow_credentials=False,
)


# ─── Compile graph once at import time ─────────────────────────────────────
# build_graph() is cheap (no I/O), but doing it at module load means the first
# request doesn't pay the compile cost.
_graph = build_graph()


# ─── Request/response models ───────────────────────────────────────────────


class SearchRequest(BaseModel):
    """Body of POST /search.

    Note that ``max_iterations`` is intentionally NOT exposed — the retry
    budget is an internal implementation detail (defaulted to 2 in
    ``Settings.default_max_iterations``), not a knob for clients to tune.
    """

    query: str = Field(..., min_length=2, description="Topic query, e.g. 'AI startups in healthcare'")
    location: str | None = Field(default=None, description="Optional location, e.g. 'Amherst, MA'")

    @field_validator("query")
    @classmethod
    def _strip_query(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("query must be at least 2 chars after trimming")
        return v


# ─── SSE serialization helpers ─────────────────────────────────────────────


def _format_sse(event_name: str, data: dict[str, Any]) -> bytes:
    """Encode one SSE record. Frontend splits on ``\\n\\n``, then on ``\\n``.

    JSON-encodes ``data`` compactly; ``json.dumps`` escapes any embedded
    newlines as ``\\n`` strings so the SSE framing isn't broken by user
    content (snippets, descriptions, etc.).
    """
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _to_wire_step(event: dict[str, Any]) -> dict[str, Any]:
    """Map the internal event shape onto the frontend's ``onStep`` contract.

    Internal events carry ``elapsed_ms`` + ``timestamp`` for richer logging;
    the JS frontend (``App.jsx``) expects ``elapsed`` and ignores
    timestamps. Drop the extras and rename so the wire format matches the
    legacy ``api/search.js`` Vercel function byte-for-byte.
    """
    return {
        "id": event["id"],
        "text": event["text"],
        "status": event["status"],
        "meta": event.get("meta"),
        "elapsed": event.get("elapsed_ms"),
    }


# ─── Streaming pipeline runner ─────────────────────────────────────────────


_SENTINEL: dict = {"__sentinel__": True}


async def _run_pipeline_stream(req: SearchRequest) -> AsyncIterator[bytes]:
    """Stream pipeline events for one request.

    Spawns the LangGraph run as a sub-task and drains the per-request event
    queue concurrently. The sub-task pattern matters: ``bind_queue`` is a
    contextvar, and we want the consumer (this generator) running outside
    the bound context so it can ``queue.get()`` without itself looking like
    it's "emitting" — but we want the graph and ALL the node tasks
    LangGraph spawns internally to see the bound queue. Wrapping the graph
    run in ``async with bind_queue(...)`` inside a child task gives us
    exactly that scope.
    """
    settings = get_settings()
    queue: asyncio.Queue = asyncio.Queue()
    holder: dict[str, Any] = {"state": None, "error": None}
    t0 = time.monotonic()

    async def runner() -> None:
        try:
            async with bind_queue(queue):
                state = initial_state(
                    query=req.query,
                    location=req.location,
                    max_iterations=settings.default_max_iterations,
                )
                # ``stream_mode="values"`` yields the full state after each
                # node; we keep the latest so we can package it into the
                # final ``result`` event below.
                async for chunk in _graph.astream(state, stream_mode="values"):
                    holder["state"] = chunk
        except Exception as e:
            logger.exception("Pipeline failed for query=%r", req.query)
            holder["error"] = e
        finally:
            await queue.put(_SENTINEL)

    runner_task = asyncio.create_task(runner())

    try:
        # Drain events as nodes emit them. The runner_task fills this queue
        # asynchronously; we yield one SSE record per pop until the sentinel.
        while True:
            event = await queue.get()
            if event is _SENTINEL:
                break
            yield _format_sse("step", _to_wire_step(event))

        # Runner finished — either with a final state or with an exception.
        if holder["error"] is not None:
            err = holder["error"]
            yield _format_sse("error", {"message": str(err) or err.__class__.__name__})
        else:
            final = holder["state"] or {}
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            yield _format_sse("result", {
                "query": req.query,
                "schema": final.get("schema", {}),
                "entities": final.get("entities", []),
                "sources": final.get("sources", []),
                "elapsed": elapsed_ms,
            })

    finally:
        # If the client disconnects mid-stream FastAPI cancels this generator;
        # propagate that to the runner so we don't leak the task or its
        # in-flight HTTP requests.
        if not runner_task.done():
            runner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner_task


# ─── Endpoints ─────────────────────────────────────────────────────────────


@app.post("/search")
async def search(req: SearchRequest) -> StreamingResponse:
    """Run the agentic pipeline and stream progress as Server-Sent Events.

    The body is parsed and validated by Pydantic before we even open the
    event stream. Pre-stream errors (e.g. invalid JSON, missing query)
    return a normal JSON 4xx — the frontend handles this branch in
    ``handleSearch`` by reading ``res.json()`` when the content-type isn't
    ``text/event-stream``.
    """
    # Surface missing ANTHROPIC_API_KEY immediately rather than failing
    # inside the first node — it gives a much clearer error in the UI.
    try:
        get_settings()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    return StreamingResponse(
        _run_pipeline_stream(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable nginx/Vercel/Cloudflare response buffering so events
            # flush in real time. Without this the browser would receive
            # all events at once when the connection closes.
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + key-presence probe.

    Reports whether each *optional* API key is configured (without
    revealing values). The required ``ANTHROPIC_API_KEY`` is checked
    separately because missing it should fail the deploy, not just
    show up in /health.
    """
    try:
        s = get_settings()
        anthropic_ok = True
    except RuntimeError:
        s = None
        anthropic_ok = False

    return {
        "status": "ok" if anthropic_ok else "degraded",
        "anthropic_key": anthropic_ok,
        "serper_key":  bool(s and s.serper_api_key),
        "tavily_key":  bool(s and s.tavily_api_key),
        "github_token": bool(s and s.github_token),
        "model": s.claude_model if s else None,
    }


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "agentic-search-backend",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
        "search": "POST /search",
    }
