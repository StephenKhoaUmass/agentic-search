"""Pluggable web-search backends.

The factory :func:`get_search_backend` picks one based on configured env
vars. Today only the Serper backend is wired; adding a Tavily MCP backend
is a one-file change: implement ``SearchBackend`` in ``tavily_mcp.py`` and
append a branch to the factory below.
"""

from __future__ import annotations

from ...config import get_settings
from .base import SearchBackend, SearchResult
from .serper import SerperBackend


__all__ = ["SearchBackend", "SearchResult", "SerperBackend", "get_search_backend"]


def get_search_backend() -> SearchBackend | None:
    """Return the first available web-search backend, or None if none is
    configured. Returning None lets the caller decide the fallback policy
    (raise, use Claude web_search, etc.) rather than baking it in here.

    Selection order (first match wins):
      1. Serper.dev — if ``SERPER_API_KEY`` is set.
      2. (TODO) Tavily MCP — if ``TAVILY_API_KEY`` is set.
    """
    settings = get_settings()

    if settings.serper_api_key:
        return SerperBackend(api_key=settings.serper_api_key)

    # TODO: Tavily MCP backend
    # if settings.tavily_api_key:
    #     from .tavily_mcp import TavilyMCPBackend
    #     return TavilyMCPBackend(api_key=settings.tavily_api_key)

    return None
