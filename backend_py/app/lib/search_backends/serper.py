"""Serper.dev (Google Search) backend.

One credit per query against ``/search``. Free tier is 2,500 credits/month.

Sequential per-query execution (not ``asyncio.gather``) is deliberate — it
matches the JS implementation, keeps Serper rate-limit risk low on the free
tier, and stays well within typical pipeline latency budgets.
"""

from __future__ import annotations

import httpx

from ...config import get_settings
from .base import SearchResult


_SEARCH_URL = "https://google.serper.dev/search"
_PER_QUERY_LIMIT = 10            # ``num`` parameter sent to Serper
_QUERIES_PER_REQUEST = 4         # mirrors queries.slice(0, 4) in agent.js


class SerperBackend:
    """``SearchBackend`` backed by https://serper.dev/search."""

    name = "serper"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search(
        self,
        queries: list[str],
        location: str | None = None,
        max_results: int = 15,
    ) -> list[SearchResult]:
        seen: set[str] = set()
        results: list[SearchResult] = []
        timeout = get_settings().http_timeout_seconds

        async with httpx.AsyncClient(timeout=timeout) as client:
            for q in queries[:_QUERIES_PER_REQUEST]:
                body: dict = {"q": q, "num": _PER_QUERY_LIMIT}
                if location:
                    body["location"] = location

                try:
                    resp = await client.post(
                        _SEARCH_URL,
                        json=body,
                        headers={
                            "X-API-KEY": self._api_key,
                            "Content-Type": "application/json",
                        },
                    )
                    if not resp.is_success:
                        # Per-query failure is non-fatal — try the next query.
                        # Total failure is observable downstream as len(sources) == 0.
                        continue
                    data = resp.json()
                except (httpx.HTTPError, ValueError):
                    continue

                for r in data.get("organic") or []:
                    url = r.get("link")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    results.append(SearchResult(
                        title=r.get("title") or "",
                        url=url,
                        snippet=r.get("snippet") or "",
                    ))

                if len(results) >= max_results:
                    break

        return results[:max_results]
