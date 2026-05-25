"""Async Jina Reader client.

Jina Reader (``r.jina.ai``) converts arbitrary web pages to clean markdown
via a simple GET request. This module is the Stage-3 scraping primitive:
fast parallel HTTP fetches with no LLM in the loop.

Failures are non-throwing — :func:`fetch_via_jina` returns ``None`` on
network errors, timeouts, or short/empty content so callers can use
``asyncio.gather(..., return_exceptions=True)`` safely.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


_JINA_READER_BASE = "https://r.jina.ai/"
_MIN_CONTENT_LEN = 50


@dataclass(frozen=True, slots=True)
class ScrapedPage:
    url: str
    title: str
    snippet: str
    content: str


async def fetch_via_jina(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_seconds: float = 15.0,
    char_limit: int = 6000,
    title_hint: str = "",
    snippet: str = "",
) -> ScrapedPage | None:
    """Fetch ``url`` through Jina Reader and return a :class:`ScrapedPage`.

    Returns ``None`` on transient failure (HTTP non-2xx, network error,
    timeout, or sub-threshold content length) so the caller doesn't need
    a try/except around every fetch.
    """
    try:
        resp = await client.get(
            f"{_JINA_READER_BASE}{url}",
            timeout=timeout_seconds,
        )
        if not resp.is_success:
            return None
        text = resp.text
    except (httpx.HTTPError, ValueError):
        return None

    if not text or len(text) < _MIN_CONTENT_LEN:
        return None

    title = title_hint or (text.split("\n", 1)[0][:100] if text else "Untitled")

    return ScrapedPage(
        url=url,
        title=title,
        snippet=snippet,
        content=text[:char_limit],
    )
