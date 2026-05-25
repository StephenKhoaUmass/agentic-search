"""Stage 3 — Parallel Page Scraping via Jina Reader.

Translates ``scrapePages`` from ``agent.js``. Async parallel HTTP fetches
with ``asyncio.gather`` and a per-page timeout. Failed pages are dropped;
if every page fails we fall back to pseudo-pages built from the search
snippets so downstream extraction still has something to chew on (matches
JS behavior).
"""

from __future__ import annotations

import asyncio

import httpx

from ...config import get_settings
from ...lib.jina import fetch_via_jina
from ...streaming.events import emit_done, emit_error, emit_running
from ..state import PipelineState


_MAX_SOURCES_TO_SCRAPE = 10


async def scrape_pages_node(state: PipelineState) -> dict:
    started = await emit_running("scrape", "Fetching page content…")

    try:
        sources = state.get("sources") or []
        if not sources:
            done = await emit_done("scrape", "No sources to scrape", started, meta="0 pages")
            return {"pages": [], "step_log": [done]}

        settings = get_settings()
        targets = sources[:_MAX_SOURCES_TO_SCRAPE]

        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
        ) as client:
            results = await asyncio.gather(
                *(
                    fetch_via_jina(
                        client,
                        src["url"],
                        timeout_seconds=settings.jina_timeout_seconds,
                        char_limit=settings.page_char_limit,
                        title_hint=src.get("title") or "",
                        snippet=src.get("snippet") or "",
                    )
                    for src in targets
                ),
                return_exceptions=True,
            )

        pages: list[dict] = []
        for r in results:
            if isinstance(r, BaseException) or r is None:
                continue
            pages.append({
                "url": r.url,
                "title": r.title,
                "snippet": r.snippet,
                "content": r.content,
            })

        # Fallback: every fetch failed → synthesize pages from snippets so the
        # extractor has at least some text to work with. Matches JS behavior.
        if not pages:
            pages = [
                {
                    "url": s["url"],
                    "title": s.get("title", ""),
                    "snippet": s.get("snippet", ""),
                    "content": s.get("snippet") or s.get("title", ""),
                }
                for s in sources
            ]

        total_chars = sum(len(p["content"]) for p in pages)
        meta = f"~{total_chars // 1000}k chars extracted"
        done = await emit_done(
            "scrape",
            f"Processed {len(pages)} pages",
            started,
            meta=meta,
        )

        return {"pages": pages, "step_log": [done]}

    except Exception as e:
        await emit_error("scrape", f"Scrape failed: {e}", started)
        raise
