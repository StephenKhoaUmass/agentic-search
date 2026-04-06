# Agentic Search — Entity Discovery Engine

A multi-stage LLM agent pipeline that takes a natural language topic query and produces a structured, source-traceable table of discovered entities.

> **Live demo:** Deployed on Vercel — API keys are stored server-side, users just enter a query.
>
> **Local:** Run `cd frontend && npm run dev` (with backend) or open `standalone/index.html` (bring your own API keys).

---

## What it does

```
Query: "AI startups in healthcare"
  ↓
[Stage 1] Schema Planner    → Generates domain-appropriate columns + 4 diverse search queries
  ↓                            (location-aware: rewrites "near me" → actual location)
[Stage 2] Web Search         → Discovers 15-20 web sources via Serper.dev (4 queries × 10 results)
  ↓      + Places Reference  → Single Google Places call for authoritative entity data
[Stage 3] Page Scraper       → Fetches page content as markdown via Jina Reader (no LLM)
  ↓
[Stage 4] Entity Extractor   → LLM extracts per-source records (temperature=0 for consistency)
  ↓                            strict qualifier filtering (e.g., "startups" excludes corporations)
[Stage 5] Enricher           → Fuzzy merge → Places cross-reference → quality scoring → filter
  ↓
Table: { name, description, headquarters, funding_stage, total_funding, website, focus_area, ... }
       each cell → traceable to source URL
```

Every cell value is attributable to a source URL. Confidence scores blend field completeness, quality signals (ratings, reviews, funding), and cross-source corroboration.

---

## Architecture

```
agentic-search/
├── api/
│   └── search.js               ← Vercel serverless function (API keys stay server-side)
├── standalone/
│   └── index.html              ← zero-dependency single-file version (user provides own keys)
├── frontend/                   ← React + Vite SPA
│   ├── .env.example            ← template for API keys (copy to .env)
│   ├── index.html
│   ├── vite.config.js
│   ├── package.json
│   └── src/
│       ├── main.jsx
│       ├── App.jsx             ← calls /api/search, renders pipeline progress + results
│       ├── App.css
│       ├── components/
│       │   ├── SearchBar.jsx
│       │   ├── PipelineProgress.jsx
│       │   ├── EntityTable.jsx  (sortable, filterable, with source links)
│       │   └── ExportButtons.jsx
│       └── lib/
│           └── agent.js        ← core 5-stage pipeline (used by api/ and backend/)
├── backend/                    ← Optional Express server for local dev
│   ├── .env.example
│   ├── src/
│   │   ├── server.js           ← Express + SSE streaming (reads keys from .env)
│   │   └── lib/pipeline.js     ← re-exports frontend agent.js
│   └── package.json
├── vercel.json                 ← Vercel deployment config
├── package.json                ← root ESM config
└── README.md
```

**Key security model:** API keys never reach the browser.
- **Vercel deployment**: `api/search.js` reads keys from Vercel Environment Variables
- **Local dev**: Express backend reads keys from `backend/.env`
- **Standalone**: user provides their own keys (runs entirely in-browser)

---

## Quick Start

### Option A — Standalone (zero dependencies, bring your own keys)

```bash
open standalone/index.html
```

Enter your Anthropic API key (and optionally Serper key) in the input fields. Keys are stored in `sessionStorage` (never sent to any server).

### Option B — Local Dev (frontend + backend, keys hidden server-side)

```bash
# 1. Set up API keys
cp backend/.env.example backend/.env
# Edit backend/.env and add your keys

# 2. Start backend (reads keys from .env)
cd backend
npm install
npm start          # → http://localhost:3001

# 3. In another terminal, start frontend
cd frontend
npm install
npm run dev        # → http://localhost:5173 (proxies /api to backend)
```

### Option C — Deploy to Vercel (recommended for sharing)

```bash
# 1. Push to GitHub
git push origin master

# 2. Import repo on Vercel (vercel.com/new)
#    - Set Root Directory to: (leave blank — uses vercel.json at root)
#    - Vercel auto-detects the build config

# 3. Add Environment Variables in Vercel dashboard:
#    ANTHROPIC_API_KEY = sk-ant-...
#    SERPER_API_KEY    = your-serper-key (optional)

# 4. Deploy — done!
```

Users visit the Vercel URL, enter a query, and results stream back in real time. Your API keys stay on the server.

---

## Approach & Design Decisions

### Why a multi-stage pipeline?

A naive "search and return results" approach yields generic, poorly-structured data. The key insight is that **different queries need different schemas**: pizza places need `cuisine, price_range, rating`; startups need `funding, stage, HQ, founded`.

The pipeline solves this with a **Schema Planner** as Stage 1 — it dynamically designs the extraction schema before any content is fetched, so the extractor knows exactly what to look for.

### Stage 1: Schema Planner (LLM)
The planner LLM call receives the query (plus optional user location) and outputs:
- `entity_type`: what kind of things we're finding
- `columns`: typed column definitions (text, url, tags, number)
- `search_queries`: 4 diverse queries targeting different source types (aggregator, structured data, niche, curated directory)
- `extraction_prompt`: domain-tailored instructions for the extraction stage, including strict entity qualification rules

If a user location is provided, "near me" and similar phrases are rewritten to include the actual location in search queries.

### Stage 2: Web Search + Places Reference (Serper.dev or Claude fallback)
**Web search**: If a Serper key is configured, uses Google search via Serper (~1-2s, native location support, 2,500 free queries). Runs 4 queries × 10 results each, deduplicates by URL, and returns up to 15 sources. Otherwise, falls back to Claude's built-in `web_search_20250305` tool (~15-30s).

**Places reference**: After web search completes, a single Serper `/places` API call fetches authoritative Google Places data (rating, review count, address, phone, price level). This runs sequentially after web search to avoid rate limiting. For non-local queries (e.g., "open source database tools"), this step is a no-op.

### Stage 3: Page Scraping (Jina Reader — no LLM)
Uses [Jina Reader](https://r.jina.ai) to fetch and convert web pages to **structured markdown** in parallel. Each page is fetched concurrently with a 15-second timeout, preserving headings, tables, and lists that contain structured data.

**Why markdown, not plain text?** Restaurant rating tables, software comparison lists, and company info boxes render as structured markdown that the LLM can parse. Plain text strips this structure.

**Why not LLM scraping?** An earlier version used Claude for scraping, adding 40-60s and costing an extra inference call. Jina Reader completes in 3-8 seconds total with deterministic output.

### Stage 4: Entity Extraction (LLM, temperature=0)
A dedicated extraction call at `temperature=0` (for deterministic output) receives the combined tagged content (~24k char cap). The extractor creates **separate records per source** for the same entity, enabling cross-source validation.

Key rules:
- **Strict qualifier matching**: entities must match ALL query qualifiers
- **Per-source records**: same entity from different sources creates separate records for cross-validation
- **Numeric parsing**: handles "4.6 stars", "47.2k", "$$$", etc.

### Stage 5: Enrichment & Ranking (pure JS)

**5a. Fuzzy Entity Merging** — per-source records are grouped using token stemming and fuzzy matching (≥70% containment, ≥2 common tokens). Attribute resolution: median for ratings, max for counts, longest string for text.

**5b. Places Cross-Reference** — corrects ratings (prefer Google's value), fills missing address/phone/price from Google Places.

**5c. Quality-Aware Scoring** — auto-detects quality signal columns, weights popularity 2x over ratings, and uses adaptive confidence (no penalty when no entities have quality data).

Fully domain-agnostic — the same code ranks pizza restaurants, startups, and software tools without any domain-specific logic.

---

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| ~6k char per-page cap | Deeply-nested page data may be truncated | Sufficient for entity extraction in practice |
| ~24k char total content cap | Only ~4-5 pages of full content reach the extractor | Chunked extraction (future work) |
| LLM extraction variance | Same query may produce slightly different results between runs | `temperature=0` for extraction; Places cross-reference for authoritative data |
| Fuzzy dedup may over-merge | Two entities with similar names could be merged | Conservative thresholds (≥2 common tokens, ≥70% containment) |
| Vercel function timeout (60s) | Very complex queries with many sources may time out | Pipeline typically completes in 20-35s |
| No caching | Every query re-runs full pipeline | Redis cache by query hash (future) |
| Location is manual | User must type their location | Browser Geolocation API (future) |

## Cost & Latency

| Mode | LLM Calls | Serper Credits | Typical Time | Est. Cost |
|------|-----------|----------------|--------------|-----------|
| Vercel (Serper + Places) | 2 | 4 search + 1 places | 20-35s | ~$0.02-0.05 |
| Local (Claude web_search) | 3 | 0 | 30-50s | ~$0.03-0.08 |
