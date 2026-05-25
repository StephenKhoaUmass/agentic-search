# Agentic Search — Entity Discovery Engine

A multi-stage LLM agent pipeline that takes a natural-language topic query and produces a structured, source-traceable table of discovered entities.

```
Query: "AI startups in healthcare"
  ↓
[1] Schema Planner   →  domain-appropriate columns + 4 diverse search queries
[2] Web Search       →  Serper.dev (or Tavily MCP, planned) → ~15 sources + Google Places refs
[3] Page Scraper     →  Jina Reader → markdown (no LLM)
[4] Entity Extractor →  Claude @ temperature=0, strict qualifier matching, per-source records
[5] Enricher         →  fuzzy merge → Places cross-walk → adaptive quality scoring → filter
  ↓
Table: { name, description, funding_stage, total_funding, headquarters, ... }   each cell ⇒ source URL
```

A LangGraph quality-gate node after extraction can route back into search with reformulated queries (max 2 iterations) when fewer than 3 sources, fewer than 5 entities, or >60% low-confidence results come back.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Node.js | ≥ 18 | for the Vite/React frontend |
| Python | ≥ 3.11 | for the LangGraph/FastAPI backend |
| `uvx` (or pip + venv) | — | recommended for managing the Python env. Install via `pip install uv` or `brew install uv` |

You'll also need API keys for:
- **Anthropic** (required) — used for the planner, extractor, and reformulator
- **Serper.dev** (optional but strongly recommended) — `https://serper.dev`, free tier is 2,500 queries/month
- **Tavily** (optional) — used via MCP when wired up; falls back to Serper today
- **GitHub PAT** (optional) — used for star-count enrichment when running against open-source queries

---

## Local setup

### 1. Backend (Python — FastAPI + LangGraph)

```bash
cd backend_py

# Fill in API keys
cp .env.example .env
$EDITOR .env       # set ANTHROPIC_API_KEY (required) and SERPER_API_KEY (recommended)

# Install dependencies (using uv — fastest)
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

# OR with plain pip
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the dev server
uvicorn app.main:app --reload --port 8000
# → http://localhost:8000           (root info)
# → http://localhost:8000/docs      (auto-generated OpenAPI UI)
# → http://localhost:8000/health    (config probe — shows which keys are present)
```

Smoke-test from the shell:

```bash
curl -N -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"open source vector databases","location":null}'
# → streams: event: step ... event: step ... event: result
```

### 2. Frontend (React + Vite)

```bash
cd frontend

cp .env.example .env
# .env contains only VITE_API_URL — points at the Python backend (default: http://localhost:8000)

npm install
npm run dev        # → http://localhost:5173
```

Open the browser, type a query (e.g. *"top pizza places near me"* with `Amherst, MA` in the Location field), and watch the pipeline stages stream in.

> **Why no Vite proxy?** The frontend talks to the backend through an absolute URL (`VITE_API_URL`) and the backend has CORS enabled (`allow_origins=["*"]` by default). This works identically in local dev and in prod — only the env var changes. The proxy block is left commented in `frontend/vite.config.js` if you ever want to hide the backend URL behind a same-origin `/api/*` path during dev.

---

## Deployment

### Frontend — Vercel

```bash
git push origin master
# Then on vercel.com/new:
#   - Framework preset: Other (or Vite if offered)
#   - Root Directory:   ./
#   - Environment variables (Vercel dashboard):
#       VITE_API_URL = https://<your-backend>.up.railway.app
```

The Vite build produces a static SPA; there are no server-side routes on Vercel anymore (the legacy `api/search.js` is kept as a reference and is not exercised by the new App).

### Backend — Railway (or Render)

The Python backend is **not** deployable to Vercel — its LangGraph runner is a long-lived async process with sub-tasks per node, which Vercel's stateless 60-second Lambda runtime can't host cleanly. Railway and Render are both happy to run a FastAPI process with persistent workers; both have a free tier sufficient for a portfolio demo.

**Railway:**

```bash
# From Railway dashboard:
#   1. New Project → Deploy from GitHub repo
#   2. Root Directory:  backend_py
#   3. Start Command:   uvicorn app.main:app --host 0.0.0.0 --port $PORT
#   4. Environment variables (Variables tab):
#        ANTHROPIC_API_KEY = sk-ant-...           (required)
#        SERPER_API_KEY    = ...                  (recommended)
#        TAVILY_API_KEY    = ...                  (optional)
#        GITHUB_PERSONAL_ACCESS_TOKEN = ...       (optional)
#        ALLOWED_ORIGINS   = https://<your-frontend>.vercel.app
#        CLAUDE_MODEL      = claude-sonnet-4-20250514   (optional override)
#   5. Generate Domain → copy the *.up.railway.app URL
```

Then set `VITE_API_URL` to that URL in your Vercel project's environment variables and redeploy the frontend.

> **Why Vercel won't work for the Python backend:** the request enters `/search`, opens an SSE stream, and the LangGraph runner emits events over a 15-60 second window while making sequential API calls to Anthropic + Serper + Jina. Vercel's serverless functions hit their 60s wall-clock limit and don't have first-class support for long-lived SSE keep-alives. Railway runs Python as a regular process behind a load balancer, so the SSE pipe stays open and the contextvar-based event queue (one per request) survives across all the node `await` points.

---

## Architecture

The pipeline is built on **LangGraph** — each of the 5 stages above is one node in a typed `StateGraph[PipelineState]`. After extraction, an `evaluate_quality` node decides `pass | retry | fail` based on source count, entity count, and the fraction of low-confidence results; on `retry` a `reformulate_queries` node asks Claude for 4 fresh search queries (deduped against prior attempts) and the graph loops back to `search_web`. Retry budget is capped at 2 iterations total to bound latency and cost. **MCP** (Model Context Protocol) tool servers are integrated through Anthropic's beta MCP API: Tavily for web search (placeholder — Serper is the default backend today) and GitHub for star-count enrichment on open-source queries. Real-time pipeline progress reaches the React frontend via **Server-Sent Events** — the FastAPI `/search` endpoint creates a per-request `asyncio.Queue` bound through a `contextvars`-scoped helper so concurrent requests have isolated event streams, and `graph.astream()` yields control to the SSE writer after each node so the UI shows `running → done` transitions in real time rather than batched at the end.

### Repo layout

```
agentic-search/
├── backend_py/                ← Python backend (LangGraph + FastAPI)  ★ primary
│   ├── app/
│   │   ├── main.py            ← FastAPI app, /search SSE endpoint, /health
│   │   ├── config.py          ← env-loading + immutable Settings dataclass
│   │   ├── graph/
│   │   │   ├── builder.py     ← LangGraph wiring + conditional retry edges
│   │   │   ├── state.py       ← PipelineState TypedDict
│   │   │   └── nodes/         ← one async function per stage
│   │   ├── lib/
│   │   │   ├── claude.py      ← Anthropic async wrapper (per-call MCP support)
│   │   │   ├── search_backends/  ← pluggable Serper / Tavily-MCP backends
│   │   │   ├── places.py      ← Serper Places API + PLACES_COL_MAP cross-walk
│   │   │   ├── jina.py        ← Jina Reader async fetch
│   │   │   ├── fuzzy_merge.py ← name normalization + merge_entities
│   │   │   └── scoring.py     ← classify_quality_columns + adaptive scoring
│   │   └── streaming/events.py ← contextvar-bound per-request queue + SSE emitters
│   ├── scripts/               ← verify_enrichment.py, verify_main_sse.py
│   ├── .env.example
│   └── requirements.txt
├── frontend/                  ← React + Vite SPA (calls Python backend via VITE_API_URL)
│   ├── src/App.jsx            ← fetch + ReadableStream SSE parser
│   ├── src/components/        ← SearchBar, PipelineProgress, EntityTable, ExportButtons
│   ├── src/lib/agent.js       ← legacy JS pipeline (kept as reference, unused by App)
│   ├── vite.config.js
│   └── .env.example
├── api/search.js              ← legacy Vercel serverless function (kept as reference)
├── standalone/index.html      ← legacy bring-your-own-keys single-file demo
├── vercel.json
└── README.md
```

### Verification scripts

Both live in `backend_py/scripts/`:

- `verify_enrichment.py` — numeric verification of fuzzy merge, places cross-walk, and the three adaptive-scoring branches (`anyEntityHasQuality` reward, penalty, and fallback).
- `verify_main_sse.py` — TestClient + stub-graph end-to-end: confirms the SSE wire format matches the frontend's `onStep` contract byte-for-byte, validates concurrent-request isolation, and exercises the pre-stream 422 branch.

Run from `backend_py/`:

```bash
ANTHROPIC_API_KEY=test-stub python -m scripts.verify_enrichment
ANTHROPIC_API_KEY=test-stub python -m scripts.verify_main_sse
```

---

## Known limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| ~6k char per-page cap | Deeply-nested page data may be truncated | Sufficient in practice for entity extraction |
| ~24k char total content cap | Only ~4-5 pages of full content reach the extractor | Chunked extraction (future) |
| LLM extraction variance | Same query may produce slightly different results between runs | `temperature=0`, Places cross-walk for authoritative numerics |
| Fuzzy dedup can over-merge | Two distinct entities with very similar names could collapse | Conservative thresholds (≥ 2 common tokens, ≥ 70% containment) |
| Retry adds 15-30s on bad runs | Worst-case latency ~60s when the first pass fails quality gate | Capped at 1 retry; quality gate also fails fast on `no_new_queries` |
| Single Serper Places call | Only covers the top result(s) per query, not every entity | Per-entity Places lookups would 5× the credits cost |
