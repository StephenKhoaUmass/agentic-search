"""Tavily MCP backend — web search via the official Tavily hosted MCP server.

Speaks the Model Context Protocol (MCP) over Streamable HTTP, using the
official ``mcp`` Python SDK. No LLM in the loop: we open a session
programmatically, call the ``tavily_search`` tool once per natural-language
query, and normalize the JSON-text response into the same
:class:`SearchResult` shape every other backend produces.

Why MCP rather than Tavily's REST API?
--------------------------------------
* The wire format is identical to other MCP servers (Brave, Exa, etc.), so
  swapping providers later is changing a URL, not rewriting glue code.
* Free MCP-side schema validation — the SDK will reject malformed args
  before they leave the process.
* Resume-grade integration: actually speaks the MCP protocol, doesn't
  just mimic the function-call shape.

Session lifecycle
-----------------
One MCP session is opened per ``.search()`` call and reused across the 4
queries — the connection handshake is non-trivial latency (~200ms) and
opening it 4× would compound. Inside the session, queries are still
sequential to mirror Serper's behavior and keep the rate-limit story
simple. The connection is torn down before ``search()`` returns.

Failure model
-------------
Conforms to the :class:`SearchBackend` contract:
* Per-query MCP errors (rate limit, bad query, tool error) → skip that
  query, continue. Never raises.
* Connection-level failures (DNS, TLS, auth) → return ``[]``. Never
  raises — caller observes ``len(sources) == 0`` and the pipeline either
  retries or degrades.

The "raises only on unrecoverable misconfiguration" clause is satisfied
because the only way to misconfigure this backend is to pass an empty
API key, which is gated by the factory.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .base import SearchResult


_TAVILY_MCP_URL = "https://mcp.tavily.com/mcp/?tavilyApiKey={key}"
_TAVILY_SEARCH_TOOL = "tavily_search"
_QUERIES_PER_REQUEST = 4         # mirrors Serper backend; keeps cost parity
_PER_QUERY_LIMIT = 10            # Tavily ``max_results`` parameter
_SNIPPET_MAX_CHARS = 280         # Tavily ``content`` blobs are long; trim for the
                                 # /search SSE source list. The full content is
                                 # still fetched by the scrape stage downstream.

_logger = logging.getLogger(__name__)


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars on a word boundary when possible."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…" if cut else text[:limit]


def _parse_tavily_text(result) -> list[dict]:
    """Tavily returns one text block containing JSON; extract ``results`` array.

    Tolerant of unexpected shapes — returns ``[]`` rather than raising, so
    per-query parse failures degrade to "no results from this query" rather
    than killing the whole search step.
    """
    for block in result.content:
        if getattr(block, "type", None) != "text":
            continue
        try:
            data = json.loads(block.text)
        except (json.JSONDecodeError, AttributeError):
            continue
        items = data.get("results")
        if isinstance(items, list):
            return items
    return []


class TavilyMCPBackend:
    """``SearchBackend`` backed by Tavily's hosted MCP server."""

    name = "tavily-mcp"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("TavilyMCPBackend requires a non-empty api_key")
        self._api_key = api_key

    async def search(
        self,
        queries: list[str],
        location: str | None = None,
        max_results: int = 15,
    ) -> list[SearchResult]:
        if not queries:
            return []

        url = _TAVILY_MCP_URL.format(key=quote(self._api_key, safe=""))
        seen: set[str] = set()
        results: list[SearchResult] = []

        try:
            async with streamablehttp_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    for q in queries[:_QUERIES_PER_REQUEST]:
                        args = {
                            "query": q,
                            "max_results": _PER_QUERY_LIMIT,
                            "search_depth": "basic",
                        }
                        # Tavily takes a 2-letter country code, not a free-form
                        # location string. "Amherst, MA" → unmappable without a
                        # gazetteer, so we just skip location enrichment here.
                        # Local-business queries still benefit from the Places
                        # call in search_web_node, which remains Serper-backed.

                        try:
                            response = await session.call_tool(
                                _TAVILY_SEARCH_TOOL, args,
                            )
                        except Exception as e:
                            _logger.warning("tavily_search call failed for %r: %s", q, e)
                            continue

                        if getattr(response, "isError", False):
                            _logger.warning(
                                "tavily_search returned isError for %r: %s",
                                q,
                                _parse_tavily_text(response) or response.content,
                            )
                            continue

                        for r in _parse_tavily_text(response):
                            url_str = r.get("url")
                            if not url_str or url_str in seen:
                                continue
                            seen.add(url_str)
                            results.append(SearchResult(
                                title=r.get("title") or "",
                                url=url_str,
                                snippet=_truncate(r.get("content") or "", _SNIPPET_MAX_CHARS),
                            ))

                        if len(results) >= max_results:
                            break

        except Exception as e:
            # Connection / handshake / auth errors. The backend contract says
            # transient failures must return [] without raising — the caller
            # observes len(sources) == 0 and the pipeline degrades or retries.
            _logger.warning("Tavily MCP session failed: %s", e)
            return []

        return results[:max_results]
