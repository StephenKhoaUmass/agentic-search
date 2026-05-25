"""Pluggable web-search backends.

The factory :func:`get_search_backend` picks one based on configured env
vars + an optional ``SEARCH_BACKEND`` override.

Default selection order (first set key wins):
    1. Tavily MCP — if ``TAVILY_API_KEY`` is set.
    2. Serper.dev — if ``SERPER_API_KEY`` is set.

Both keys at once is fine — Tavily takes priority for the *search* path
because it returns richer pre-processed page content (and we want the
real MCP integration exercised by default). Serper continues to drive
the *Places* call independently (see ``search_web_node``); Tavily has no
local-business equivalent today.

To force a specific backend, set ``SEARCH_BACKEND=serper`` or
``SEARCH_BACKEND=tavily`` in the env.
"""

from __future__ import annotations

from ...config import get_settings
from .base import SearchBackend, SearchResult
from .serper import SerperBackend


__all__ = ["SearchBackend", "SearchResult", "SerperBackend", "get_search_backend"]


def get_search_backend() -> SearchBackend | None:
    """Return the first available web-search backend, or ``None`` if none is
    configured. Returning ``None`` lets the caller decide the fallback policy
    (raise, use Claude web_search, etc.) rather than baking it in here.
    """
    settings = get_settings()
    override = (settings.search_backend or "").strip().lower() or None

    if override == "serper" and settings.serper_api_key:
        return SerperBackend(api_key=settings.serper_api_key)
    if override == "tavily" and settings.tavily_api_key:
        from .tavily_mcp import TavilyMCPBackend
        return TavilyMCPBackend(api_key=settings.tavily_api_key)

    # No (or unrecognized) override → preferred-order auto-select.
    if settings.tavily_api_key:
        from .tavily_mcp import TavilyMCPBackend
        return TavilyMCPBackend(api_key=settings.tavily_api_key)
    if settings.serper_api_key:
        return SerperBackend(api_key=settings.serper_api_key)

    return None
