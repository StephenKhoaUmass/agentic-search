"""LangGraph state for the agentic search pipeline.

Notes on update semantics
-------------------------
LangGraph merges each node's return dict into the running state. By default a
returned value REPLACES the existing one. For fields that should accumulate
across nodes/iterations we annotate them with ``operator.add`` so LangGraph
appends instead of overwriting.

- ``step_log`` is appended to by every node (UI streams these as SSE events).
- ``reformulated_queries`` is appended to once per retry loop so the
  ``reformulate_queries_node`` can dedupe new candidates against prior history.

Everything else (``sources``, ``entities``, ``schema``, ...) is replaced on each
update, which is what we want — a retry iteration should fully overwrite the
previous run's intermediate data.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, Literal, TypedDict


QualityVerdict = Literal["pending", "pass", "retry", "fail"]
QualityReason = Literal[
    "too_few_sources",
    "too_few_entities",
    "low_confidence",
    "exhausted_retries",
    "no_new_queries",
]


class StepLogEntry(TypedDict, total=False):
    """One pipeline progress event. Streamed to the frontend as SSE."""

    node: str                       # node name, e.g. "search_web"
    id: str                         # stable id used by the React UI (matches agent.js)
    text: str                       # human-readable message
    status: Literal["running", "done", "error"]
    timestamp: str                  # ISO-8601 UTC
    meta: str | None                # optional secondary line shown in UI
    elapsed_ms: int | None          # only on terminal status


class PipelineState(TypedDict, total=False):
    """Mutable state passed between LangGraph nodes."""

    # ── Inputs ──────────────────────────────────────────────────────────────
    query: str
    location: str | None

    # ── Loop control ────────────────────────────────────────────────────────
    iteration: int                  # 0 on first run, incremented by reformulate_queries
    max_iterations: int             # configured per-request; default 2

    # ── Pipeline data (replaced each iteration) ─────────────────────────────
    schema: dict[str, Any]          # planner output: entity_type, columns, search_queries, extraction_prompt
    sources: list[dict[str, Any]]   # [{title, url, snippet}, ...]
    places_ref: list[dict[str, Any]]  # Serper Places authoritative data (internal — not returned to client)
    pages: list[dict[str, Any]]     # scraped markdown blobs
    raw_entities: list[dict[str, Any]]  # per-source extracted records, before fuzzy merge
    entities: list[dict[str, Any]]  # final ranked + merged result rows

    # ── Quality control ─────────────────────────────────────────────────────
    quality_verdict: QualityVerdict
    quality_reason: QualityReason | None

    # History of query sets tried by the planner / reformulator, in order.
    # Used by ``reformulate_queries_node`` to avoid asking Claude for queries
    # we already used. Append-only.
    reformulated_queries: Annotated[list[list[str]], add]

    # ── Observability (append-only) ─────────────────────────────────────────
    step_log: Annotated[list[StepLogEntry], add]
    elapsed_ms: int                 # total wall-clock, written at end


def initial_state(query: str, location: str | None = None, max_iterations: int = 2) -> PipelineState:
    """Factory for a fresh state dict at the start of a request."""
    return PipelineState(
        query=query,
        location=location,
        iteration=0,
        max_iterations=max_iterations,
        schema={},
        sources=[],
        places_ref=[],
        pages=[],
        raw_entities=[],
        entities=[],
        quality_verdict="pending",
        quality_reason=None,
        reformulated_queries=[],
        step_log=[],
        elapsed_ms=0,
    )
