"""Hybrid backend — Tavily MCP + Serper.dev in parallel, results interleaved.

Why hybrid
----------
Empirically the two backends discover *complementary* URL sets for the
same query. Snapshot from a 3-query benchmark:

* Tavily favors blog-format and academic sources (arXiv surveys,
  Medium/Zilliz-style comparison posts). Strong on focused commentary,
  weak on long-tail enumeration.
* Serper surfaces GitHub awesome-lists and community-curated directories
  (e.g. ``awesome-vector-database``) that include niche-but-real
  entries Tavily's ranking deprioritizes.

Running both and unioning the results recovers the long-tail entities
(Vespa, pgvector in the vector-DB benchmark) without giving up Tavily's
GitHub-URL-rich results (which feed the direct-slug path in
``github_enrich.py`` and lift GH fill rate).

Latency profile
---------------
Wall-clock is ``max(tavily_time, serper_time)``, not the sum — the two
backends fire concurrently through ``asyncio.gather``. The result-list
is capped at ``max_results`` like any single backend, so the downstream
scrape stage sees the same fan-out (no fanout amplification).

Ordering — interleaved round-robin
----------------------------------
Tavily and Serper each return their own ranked list. Interleaving them
``[t0, s0, t1, s1, …]`` gives both backends equal weight at the top of
the merged feed regardless of how many results each returned. URLs that
appear in BOTH backend lists float to where they first appeared (since
both backends ranked them highly), which is the cheapest available
signal of "both engines agree this is relevant".

Failure model — graceful per-backend degradation
------------------------------------------------
Each backend call is wrapped in ``asyncio.gather(..., return_exceptions=True)``.
If one backend raises (network, auth, MCP handshake failure), the other
backend's results are still returned. Only when *both* fail do we return
an empty list — same contract as single-backend implementations.
"""

from __future__ import annotations

import asyncio
import logging
from itertools import zip_longest

from .base import SearchBackend, SearchResult


_logger = logging.getLogger(__name__)


class HybridBackend:
    """``SearchBackend`` that runs Tavily MCP and Serper concurrently."""

    name = "hybrid"

    def __init__(self, tavily: SearchBackend, serper: SearchBackend) -> None:
        self._tavily = tavily
        self._serper = serper

    async def search(
        self,
        queries: list[str],
        location: str | None = None,
        max_results: int = 15,
    ) -> list[SearchResult]:
        # Concurrent fan-out. ``return_exceptions=True`` so one backend
        # crashing doesn't cancel the other; we degrade to single-backend
        # results rather than failing the whole search step.
        tavily_results, serper_results = await asyncio.gather(
            self._tavily.search(queries, location=location, max_results=max_results),
            self._serper.search(queries, location=location, max_results=max_results),
            return_exceptions=True,
        )

        if isinstance(tavily_results, BaseException):
            _logger.warning("Tavily failed in hybrid backend: %r", tavily_results)
            tavily_results = []
        if isinstance(serper_results, BaseException):
            _logger.warning("Serper failed in hybrid backend: %r", serper_results)
            serper_results = []

        # Round-robin interleave with URL-level dedup. Stops as soon as
        # ``max_results`` is filled — no work wasted on tail entries that
        # would be trimmed anyway.
        seen: set[str] = set()
        merged: list[SearchResult] = []
        for t, s in zip_longest(tavily_results, serper_results):
            for r in (t, s):
                if r is None or r.url in seen:
                    continue
                seen.add(r.url)
                merged.append(r)
                if len(merged) >= max_results:
                    return merged

        return merged
