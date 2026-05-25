"""End-to-end SSE wire-format verification.

Runs FastAPI's TestClient against /search with all node coroutines replaced
by lightweight stubs. Asserts:

  * Step events have shape ``{id, text, status, meta, elapsed}`` and ONLY
    those keys (no leaked ``elapsed_ms`` / ``timestamp``).
  * Each SSE record is properly framed (``event: <name>\\n``,
    ``data: <json>\\n``, ``\\n``).
  * The final ``result`` event has ``{query, schema, entities, sources,
    elapsed}`` and contains the data accumulated through the graph.
  * /health reports a sane shape.
  * Pydantic validation (e.g. 1-char query) returns 422 BEFORE the stream
    opens (so the frontend's pre-stream JSON-error branch works).

Run from ``backend_py/``::

    ANTHROPIC_API_KEY=test-stub python -m scripts.verify_main_sse
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A valid-looking key is enough to satisfy get_settings() — we stub every node
# so the actual Anthropic client is never invoked.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-stub")

from app.graph.nodes.enrich_entities import enrich_entities_node as real_enrich_entities_node  # noqa: E402
from app.graph.state import PipelineState  # noqa: E402
from app.streaming.events import emit_done, emit_running  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402


async def _stub_plan(state):
    started = await emit_running("plan", "Planning schema…")
    schema = {
        "entity_type": "test entities",
        "columns": [
            {"key": "name", "type": "text"},
            {"key": "description", "type": "text"},
            {"key": "rating", "type": "number"},
            {"key": "source_url", "type": "text"},
        ],
        "search_queries": ["test query 1", "test query 2"],
        "extraction_prompt": "Extract test entities.",
    }
    done = await emit_done("plan", "Schema ready", started, meta="4 cols, 2 queries")
    return {"schema": schema, "reformulated_queries": [list(schema["search_queries"])],
            "step_log": [done]}


async def _stub_search(state):
    started = await emit_running("search", "Searching web…")
    sources = [
        {"title": "Source A", "url": "https://example.com/a", "snippet": "stub"},
        {"title": "Source B", "url": "https://example.com/b", "snippet": "stub"},
    ]
    done = await emit_done("search", "Found 2 sources", started, meta="backend=stub")
    return {"sources": sources, "places_ref": [], "step_log": [done]}


async def _stub_scrape(state):
    started = await emit_running("scrape", "Scraping pages…")
    pages = [{"url": s["url"], "title": s["title"], "content": "stub content body"} for s in state["sources"]]
    done = await emit_done("scrape", "Scraped 2 pages", started)
    return {"pages": pages, "step_log": [done]}


async def _stub_extract(state):
    started = await emit_running("extract", "Extracting entities…")
    raw = [
        {"name": "Acme Co", "description": "Acme makes stuff", "rating": 4.5, "source_url": "https://example.com/a"},
        {"name": "Beta Inc", "description": "Beta also makes stuff", "rating": 4.0, "source_url": "https://example.com/b"},
    ]
    done = await emit_done("extract", "Extracted 2 records", started)
    return {"raw_entities": raw, "step_log": [done]}


async def _stub_evaluate(state):
    started = await emit_running("evaluate", "Evaluating quality…")
    done = await emit_done("evaluate", "Quality: pass", started)
    return {"quality_verdict": "pass", "step_log": [done]}


# ─── Build a stub-only graph and inject it into main ────────────────────────
# Compiling the real graph at main.py import time captures node function
# *references* (not lazy lookups), so `mock.patch` after the fact is a no-op.
# Cleaner: import main, then replace its private `_graph` attribute with a
# graph built from our stubs. enrich_entities_node uses the real impl
# (it's pure logic, no network). reformulate_queries_node is unreachable
# because _stub_evaluate returns verdict='pass'.

def _build_stub_graph():
    g: StateGraph = StateGraph(PipelineState)
    g.add_node("plan_schema",      _stub_plan)
    g.add_node("search_web",       _stub_search)
    g.add_node("scrape_pages",     _stub_scrape)
    g.add_node("extract_entities", _stub_extract)
    g.add_node("enrich_entities",  real_enrich_entities_node)
    g.add_node("evaluate_quality", _stub_evaluate)
    g.add_edge(START, "plan_schema")
    g.add_edge("plan_schema",      "search_web")
    g.add_edge("search_web",       "scrape_pages")
    g.add_edge("scrape_pages",     "extract_entities")
    g.add_edge("extract_entities", "enrich_entities")
    g.add_edge("enrich_entities",  "evaluate_quality")
    g.add_edge("evaluate_quality", END)
    return g.compile()


import app.main as main_mod  # noqa: E402
main_mod._graph = _build_stub_graph()
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def hr(title: str) -> None:
    print(f"\n{'═' * 70}\n  {title}\n{'═' * 70}")


# Helper: parse the raw SSE body the way App.jsx does
def parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for part in body.split("\n\n"):
        if not part.strip():
            continue
        event_name = ""
        data_str = ""
        for line in part.split("\n"):
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                data_str = line[6:]
        if event_name and data_str:
            events.append((event_name, json.loads(data_str)))
    return events


# ════════════════════════════════════════════════════════════════════════════
client = TestClient(app)

# ─── PART 1: /health ─────────────────────────────────────────────────────────
hr("PART 1 — GET /health")

r = client.get("/health")
print(f"  status: {r.status_code}")
print(f"  body:   {r.json()}")
assert r.status_code == 200
body = r.json()
assert body["status"] == "ok"
assert body["anthropic_key"] is True   # we set ANTHROPIC_API_KEY=test-stub
assert "serper_key" in body and "tavily_key" in body
print("  PART 1: OK")


# ─── PART 2: /search happy path — full pipeline, stubbed nodes ──────────────
hr("PART 2 — POST /search (happy path)")

with client.stream("POST", "/search", json={"query": "ai startups in healthcare"}) as response:
    print(f"  status:        {response.status_code}")
    print(f"  content-type:  {response.headers.get('content-type')}")
    print(f"  cache-control: {response.headers.get('cache-control')}")
    print(f"  x-accel-buf:   {response.headers.get('x-accel-buffering')}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    body_text = "".join(chunk for chunk in response.iter_text())

events = parse_sse(body_text)
print(f"\n  Parsed {len(events)} SSE events:")
for name, data in events:
    if name == "step":
        print(f"    [{name}]  id={data.get('id'):<10} status={data.get('status'):<8} text={data.get('text')!r}")
    elif name == "result":
        print(f"    [{name}] query={data.get('query')!r} entities={len(data.get('entities', []))}"
              f" sources={len(data.get('sources', []))} elapsed={data.get('elapsed')}ms")
        print(f"             schema.entity_type={data.get('schema', {}).get('entity_type')!r}")
    elif name == "error":
        print(f"    [{name}]  message={data.get('message')!r}")

step_events = [data for name, data in events if name == "step"]
result_events = [data for name, data in events if name == "result"]
error_events = [data for name, data in events if name == "error"]

# Expect step events for each node, then exactly one result, no errors.
assert len(step_events) >= 10, f"expected many step events, got {len(step_events)}"
assert len(result_events) == 1, f"expected 1 result event, got {len(result_events)}"
assert len(error_events) == 0, f"expected 0 error events, got {len(error_events)}"

# ── Wire shape: step events must have EXACTLY these keys ────────────────────
# (no elapsed_ms, no timestamp leaking through)
EXPECTED_STEP_KEYS = {"id", "text", "status", "meta", "elapsed"}
for s in step_events:
    extra = set(s.keys()) - EXPECTED_STEP_KEYS
    missing = EXPECTED_STEP_KEYS - set(s.keys())
    assert not extra,   f"unexpected keys in step event: {extra} (full: {s})"
    assert not missing, f"missing keys in step event: {missing} (full: {s})"
print(f"\n  All {len(step_events)} step events have exactly {sorted(EXPECTED_STEP_KEYS)}: OK")

# Each node should have at least a running + done pair
node_ids = {s["id"] for s in step_events}
print(f"  Distinct step ids: {sorted(node_ids)}")
assert {"plan", "search", "scrape", "extract", "enrich", "evaluate"} <= node_ids

# running → done ordering for each node
for nid in node_ids:
    for_node = [s for s in step_events if s["id"] == nid]
    statuses = [s["status"] for s in for_node]
    assert statuses[0] == "running" and "done" in statuses, f"bad order for {nid}: {statuses}"
print("  running→done ordering correct for each node: OK")

# elapsed must be None on running, an int on done
for s in step_events:
    if s["status"] == "running":
        assert s["elapsed"] is None, f"running event leaked elapsed: {s}"
    elif s["status"] == "done":
        assert isinstance(s["elapsed"], int) and s["elapsed"] >= 0, f"done event has bad elapsed: {s}"
print("  elapsed=None on running, int on done: OK")

# ── Result event shape ──────────────────────────────────────────────────────
result = result_events[0]
EXPECTED_RESULT_KEYS = {"query", "schema", "entities", "sources", "elapsed"}
extra = set(result.keys()) - EXPECTED_RESULT_KEYS
missing = EXPECTED_RESULT_KEYS - set(result.keys())
assert not extra,   f"unexpected keys in result: {extra}"
assert not missing, f"missing keys in result: {missing}"
assert result["query"] == "ai startups in healthcare"
assert isinstance(result["schema"], dict) and result["schema"].get("entity_type")
assert isinstance(result["entities"], list)
assert isinstance(result["sources"], list) and len(result["sources"]) == 2
assert isinstance(result["elapsed"], int) and result["elapsed"] >= 0
print(f"  Result has exactly {sorted(EXPECTED_RESULT_KEYS)}: OK")
print("  PART 2: OK")


# ─── PART 3: /search validation (1-char query) ──────────────────────────────
hr("PART 3 — POST /search (validation: too-short query)")

r = client.post("/search", json={"query": "x"})
print(f"  status: {r.status_code}")
print(f"  body:   {r.json()}")
assert r.status_code == 422, "Pydantic should reject 1-char query BEFORE stream opens"
# The frontend reads res.json() when content-type isn't text/event-stream — works.
assert r.headers.get("content-type", "").startswith("application/json")
print("  PART 3: OK — pre-stream 422 (frontend's non-SSE branch fires)")


# ─── PART 4: Concurrent requests don't cross-wire event streams ─────────────
# Spawn two simultaneous /search calls with different queries; assert each
# result event reflects its own query. If the contextvar leaked, they'd
# both show the same query (and likely interleaved step events).

hr("PART 4 — Concurrent requests have isolated event streams")

import asyncio
import httpx
from httpx import ASGITransport


async def fire(client_a: httpx.AsyncClient, query: str) -> list[tuple[str, dict]]:
    async with client_a.stream("POST", "http://test/search", json={"query": query}) as resp:
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk
    return parse_sse(body)


async def race():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r1, r2 = await asyncio.gather(
            fire(ac, "concurrent query alpha"),
            fire(ac, "concurrent query beta"),
        )
    return r1, r2


r1, r2 = asyncio.run(race())

q1 = [data["query"] for name, data in r1 if name == "result"][0]
q2 = [data["query"] for name, data in r2 if name == "result"][0]
print(f"  request 1 result.query = {q1!r}")
print(f"  request 2 result.query = {q2!r}")
assert q1 == "concurrent query alpha"
assert q2 == "concurrent query beta"
# Each stream should have its own full set of step events
assert len([1 for name, _ in r1 if name == "step"]) >= 10
assert len([1 for name, _ in r2 if name == "step"]) >= 10
print("  PART 4: OK — contextvar isolation works under concurrency")


print("\n" + "═" * 70)
print("  ALL VERIFICATION GROUPS PASSED")
print("═" * 70)
