"""Anthropic API wrapper for the agentic search pipeline.

Design contract
---------------
1. **One async client per process.** ``AsyncAnthropic`` is lazily constructed
   and reused. Constructing it per call would leak connection pools.

2. **All call-time options pass per call, never per client.** This matters
   most for ``mcp_servers``: different nodes can wire in different MCP
   servers (Tavily for search, GitHub for enrichment) without rebuilding
   the client. Per the Anthropic SDK, ``mcp_servers`` is a parameter of
   ``messages.create()``, not of ``AsyncAnthropic(...)``.

3. **MCP routing is opt-in per call.** When ``mcp_servers`` is non-empty we
   route through the Beta endpoint (``client.beta.messages.create``) and
   pass the required beta header. Otherwise we use the standard endpoint
   so non-MCP calls (planner, extractor, reformulator) don't pay any
   beta-API surface area.

4. **Two return shapes:** ``call_claude`` returns concatenated text content
   for the common JSON-extraction case (mirrors the JS ``callClaude``);
   ``call_claude_raw`` returns the full SDK ``Message`` for callers that
   need to inspect ``tool_use`` / ``mcp_tool_use`` blocks (enrichment node).
"""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import Message

from ..config import get_settings


# Beta header required by the SDK when sending ``mcp_servers``.
# Pinned here so MCP wiring isn't scattered across nodes — update in one place
# when Anthropic GAs the MCP connector.
_MCP_BETA_HEADER = "mcp-client-2025-04-04"


# ─── Singleton async client ──────────────────────────────────────────────────

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    """Return the process-wide ``AsyncAnthropic`` client, building it lazily."""
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


# ─── Public API ──────────────────────────────────────────────────────────────


async def call_claude_raw(
    *,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 2000,
    temperature: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> Message:
    """Call Claude and return the full SDK ``Message`` (all blocks intact).

    Use this when the caller needs to inspect ``tool_use`` or
    ``mcp_tool_use`` content blocks (e.g. ``enrich_entities_node``
    consuming GitHub MCP results). Prefer ``call_claude`` otherwise.
    """
    settings = get_settings()
    client = get_client()

    payload: dict[str, Any] = {
        "model": model or settings.claude_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools

    if mcp_servers:
        return await client.beta.messages.create(
            **payload,
            mcp_servers=mcp_servers,
            betas=[_MCP_BETA_HEADER],
        )
    return await client.messages.create(**payload)


async def call_claude(
    *,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 2000,
    temperature: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    mcp_servers: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> str:
    """Call Claude and return concatenated text-block content.

    Convenience wrapper around :func:`call_claude_raw` for the common case
    where the caller only needs the assistant's textual JSON output and
    doesn't care about tool-use bookkeeping. Mirrors the return contract of
    the JS ``callClaude`` in ``agent.js``.
    """
    msg = await call_claude_raw(
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=tools,
        mcp_servers=mcp_servers,
        model=model,
    )
    return "".join(
        getattr(block, "text", "")
        for block in msg.content
        if getattr(block, "type", None) == "text"
    )
