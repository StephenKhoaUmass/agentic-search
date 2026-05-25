"""Stage 2 — Web Search + Places Reference.

Discovers candidate web sources for the current query, plus (optionally)
authoritative Google Places data for entity cross-referencing during
enrichment.

Backend selection is pluggable via
``app.lib.search_backends.get_search_backend()``. Today only the
:class:`SerperBackend` is wired up; the abstraction is designed so adding a
Tavily MCP backend is a one-file change:

    1. Implement ``SearchBackend`` in
       ``app/lib/search_backends/tavily_mcp.py``.
    2. Append a branch to ``get_search_backend()`` that returns it when
       ``TAVILY_API_KEY`` is set.

No changes to this node, the state, or any other pipeline stage are needed.

Places reference is intentionally NOT part of the SearchBackend abstraction:
Google Places is Serper-specific, runs sequentially after the main search
to avoid concurrent rate-limit hits, and is gated on whether the schema
actually has columns the PLACES_COL_MAP can cross-walk. For non-local
queries (e.g. "open source vector databases") this step is a no-op.
"""

from __future__ import annotations

from ...config import get_settings
from ...lib.places import fetch_serper_places, has_mappable_columns
from ...lib.search_backends import get_search_backend
from ...streaming.events import emit_done, emit_error, emit_running
from ..state import PipelineState


async def search_web_node(state: PipelineState) -> dict:
    started = await emit_running("search", "Discovering web sources…")

    try:
        schema = state["schema"]
        queries = list(schema.get("search_queries") or [])
        if not queries:
            raise ValueError("Schema has no search_queries — was plan_schema_node skipped?")

        location = state.get("location")

        # ── 2a. Main web search via the configured backend ──────────────────
        backend = get_search_backend()
        if backend is None:
            raise RuntimeError(
                "No web search backend configured. Set SERPER_API_KEY in "
                "backend_py/.env (Tavily MCP backend not yet wired)."
            )

        search_results = await backend.search(queries, location=location)
        sources = [
            {"title": s.title, "url": s.url, "snippet": s.snippet}
            for s in search_results
        ]

        # ── 2b. Optional Places reference (Serper-only, sequential) ─────────
        # Gated on:
        #   • Serper API key present (Tavily backend has no Places equivalent)
        #   • Schema has at least one column PLACES_COL_MAP can cross-walk
        # Running sequentially after the main search avoids hitting Serper
        # with concurrent /search and /places requests on the free tier.
        places_ref: list[dict] = []
        settings = get_settings()
        if settings.serper_api_key and has_mappable_columns(schema):
            places_ref = await fetch_serper_places(
                query=state["query"],
                location=location,
                api_key=settings.serper_api_key,
                timeout_seconds=settings.http_timeout_seconds,
            )

        meta = (
            f"backend={backend.name}"
            + (f" · {len(places_ref)} Places refs" if places_ref else " · Places: n/a")
        )
        done = await emit_done(
            "search",
            f"Found {len(sources)} sources",
            started,
            meta=meta,
        )

        return {
            "sources": sources,
            "places_ref": places_ref,
            "step_log": [done],
        }

    except Exception as e:
        await emit_error("search", f"Search failed: {e}", started)
        raise
