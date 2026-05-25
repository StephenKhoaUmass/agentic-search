"""Pluggable web-search backend protocol.

Concrete implementations live in sibling modules (e.g. ``serper.py``,
``tavily_mcp.py``). The protocol is intentionally narrow so an MCP-backed
backend can satisfy it just as easily as a direct REST one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One web search result, normalized across backends."""

    title: str
    url: str
    snippet: str


class SearchBackend(Protocol):
    """A backend turns a list of natural-language queries into a deduplicated
    list of :class:`SearchResult` records.

    Contract for implementations:
      - Run all provided queries (subject to internal per-backend caps).
      - Deduplicate results by URL across queries.
      - Return up to ``max_results`` items in priority order.
      - Return ``[]`` on transient failure; do NOT raise on per-query
        failure — soldier on and return what was collected. Raising is
        reserved for unrecoverable misconfiguration.
    """

    name: str

    async def search(
        self,
        queries: list[str],
        location: str | None = None,
        max_results: int = 15,
    ) -> list[SearchResult]: ...
