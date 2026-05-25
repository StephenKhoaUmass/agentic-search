"""Pluggable web-search backends.

The factory :func:`get_search_backend` picks one based on configured env
vars + an optional ``SEARCH_BACKEND`` override.

Default selection (auto, when ``SEARCH_BACKEND`` is unset):
    1. Hybrid — if BOTH ``TAVILY_API_KEY`` and ``SERPER_API_KEY`` are set.
       Runs the two backends in parallel via ``asyncio.gather`` and
       interleaves the results. Recovers complementary long-tail URLs
       (Tavily favors blog/arxiv content, Serper surfaces GitHub
       awesome-lists) at no real latency cost — wall-clock is
       ``max(tavily, serper)`` rather than the sum.
    2. Tavily MCP — if only ``TAVILY_API_KEY`` is set.
    3. Serper.dev — if only ``SERPER_API_KEY`` is set.

Explicit overrides via ``SEARCH_BACKEND=hybrid|tavily|serper`` always
beat auto-select and fall through to a single backend if the requested
combination isn't possible (e.g. ``hybrid`` with only one key set will
silently use whichever single backend is configured).

Serper drives the Places call inside ``search_web_node`` regardless of
the search-path choice — Tavily has no local-business equivalent today.
"""

from __future__ import annotations

from ...config import get_settings
from .base import SearchBackend, SearchResult
from .serper import SerperBackend


__all__ = ["SearchBackend", "SearchResult", "SerperBackend", "get_search_backend"]


def _make_tavily(api_key: str) -> SearchBackend:
    """Lazy import so projects without the ``mcp`` package can still load
    this module (Serper-only deployments)."""
    from .tavily_mcp import TavilyMCPBackend
    return TavilyMCPBackend(api_key=api_key)


def _make_hybrid(tavily_key: str, serper_key: str) -> SearchBackend:
    from .hybrid import HybridBackend
    return HybridBackend(
        tavily=_make_tavily(tavily_key),
        serper=SerperBackend(api_key=serper_key),
    )


def get_search_backend() -> SearchBackend | None:
    """Return a configured web-search backend, or ``None`` if no keys are
    set. Returning ``None`` lets the caller decide the fallback policy
    (raise, use Claude web_search, etc.) rather than baking it in here.
    """
    settings = get_settings()
    has_tavily = bool(settings.tavily_api_key)
    has_serper = bool(settings.serper_api_key)
    override = (settings.search_backend or "").strip().lower() or None

    # Explicit override → honored when the required key(s) are set;
    # otherwise we fall through to auto-select.
    if override == "hybrid" and has_tavily and has_serper:
        return _make_hybrid(settings.tavily_api_key, settings.serper_api_key)
    if override == "tavily" and has_tavily:
        return _make_tavily(settings.tavily_api_key)
    if override == "serper" and has_serper:
        return SerperBackend(api_key=settings.serper_api_key)

    # Auto-select: hybrid when possible, else single backend.
    if has_tavily and has_serper:
        return _make_hybrid(settings.tavily_api_key, settings.serper_api_key)
    if has_tavily:
        return _make_tavily(settings.tavily_api_key)
    if has_serper:
        return SerperBackend(api_key=settings.serper_api_key)

    return None
