# Agentic Search — Entity Discovery Engine

A multi-stage LLM agent pipeline that takes a natural language topic query and produces a structured, source-traceable table of discovered entities.

> **Try it:** Open `standalone/index.html` in your browser with your Anthropic API key.

---

## What it does

```
Query: "AI startups in healthcare"
  ↓
[Stage 1] Schema Planner    → Generates domain-appropriate columns + search queries
  ↓                            (location-aware: rewrites "near me" → actual location)
[Stage 2] Web Search Agent  → Discovers 6-12 relevant web sources via Claude web_search tool
  ↓
[Stage 3] Page Scraper      → Fetches page content in parallel via Jina Reader (no LLM)
  ↓
[Stage 4] Entity Extractor  → LLM extracts structured records with source attribution
  ↓
[Stage 5] Enricher          → Deduplication, confidence scoring, normalization
  ↓
Table: { name, description, headquarters, funding_stage, total_funding, website, focus_area, ... }
       each cell → traceable to source URL
```

Every cell value is attributable to a source URL. Confidence scores blend field completeness with quality signals (ratings, reviews) when available.

---

## Architecture

```
agentic-search/
├── standalone/
│   └── index.html              ← zero-dependency single-file version (start here)
├── frontend/                   ← React + Vite SPA
│   ├── index.html              ← Vite entry point
│   ├── vite.config.js
│   ├── package.json
│   └── src/
│       ├── main.jsx            ← React root mount
│       ├── App.jsx             ← top-level app with API key + location + search UI
│       ├── App.css             ← dark-theme styles
│       ├── components/
│       │   ├── SearchBar.jsx
│       │   ├── PipelineProgress.jsx
│       │   ├── EntityTable.jsx  (sortable, filterable, with source links)
│       │   └── ExportButtons.jsx
│       └── lib/
│           └── agent.js        ← core 5-stage pipeline (shared with backend)
├── backend/                    ← Optional Express API (server-side key handling)
│   ├── src/
│   │   ├── server.js           ← Express + SSE streaming
│   │   └── lib/pipeline.js     ← re-exports frontend agent.js
│   └── package.json
└── README.md
```

---

## Quick Start

### Option A — Standalone (zero dependencies)

```bash
# Just open the file in your browser
open standalone/index.html
# or: double-click it in Finder/Explorer
```

Enter your Anthropic API key in the UI (stored in sessionStorage only). Optionally set your location for "near me" queries. Run a query.

### Option B — Frontend Dev Server

```bash
cd frontend
npm install
npm run dev        # → http://localhost:5173
```

The React frontend calls the Anthropic API directly from the browser (same as standalone).

### Option C — With Backend (fastest — Serper + server-side keys)

```bash
# Terminal 1: backend
cd backend
npm install
ANTHROPIC_API_KEY=sk-ant-... SERPER_API_KEY=... npm start   # → http://localhost:3001

# Terminal 2: frontend pointed at backend
cd frontend
VITE_API_URL=http://localhost:3001 npm run dev
```

With `SERPER_API_KEY` set, the backend uses [serper.dev](https://serper.dev) for web search (faster, location-aware) instead of Claude's web_search tool. Without it, falls back to Claude's web_search automatically. Free tier: 2,500 queries.

---

## Approach & Design Decisions

### Why a multi-stage pipeline?

A naive "search and return results" approach yields generic, poorly-structured data. The key insight is that **different queries need different schemas**: pizza places need `cuisine, price_range, neighborhood`; startups need `funding, stage, HQ, founded`.

The pipeline solves this with a **Schema Planner** as Stage 1 — it dynamically designs the extraction schema before any content is fetched, so the extractor knows exactly what to look for.

### Stage 1: Schema Planner (LLM)
The planner LLM call receives the query (plus optional user location) and outputs:
- `entity_type`: what kind of things we're finding
- `columns`: typed column definitions (text, url, tags, number)
- `search_queries`: 3 diverse queries to maximize source diversity (broad, specific, listicle)
- `extraction_prompt`: domain-tailored instructions for the extraction stage

If a user location is provided, "near me" and similar phrases are rewritten to include the actual location in search queries.

### Stage 2: Web Search (Serper.dev or Claude web_search fallback)
If a [serper.dev](https://serper.dev) key is provided, uses Google search via Serper (~1-2s, native location support, 2,500 free queries). Otherwise, falls back to Claude's built-in `web_search_20250305` tool (~15-30s, no extra API key needed).

Serper runs 3 queries (from the Schema Planner's `search_queries`) in sequence, deduplicates by URL, and returns up to 15 sources. The `location` parameter is passed directly to Google for geographically relevant results.

**Trade-off**: Serper requires a second API key. In browser-only mode, it may fail due to CORS restrictions — the pipeline automatically falls back to Claude's web_search.

### Stage 3: Page Scraping (Jina Reader — no LLM)
Uses [Jina Reader](https://r.jina.ai) to fetch and convert web pages to clean text in parallel. Each page is fetched concurrently with a 15-second timeout, producing clean markdown without any HTML parsing.

**Why not LLM scraping?** An earlier version used a Claude call with `web_search` to "scrape" pages, but this was:
- **Slow**: added 40-60s to the pipeline (the single slowest stage)
- **Expensive**: an entire LLM inference call for content that doesn't need intelligence
- **Inconsistent**: Claude's summarization of pages varied widely in quality

Jina Reader eliminates this bottleneck entirely — parallel HTTP fetches complete in 3-8 seconds total, with deterministic output.

### Stage 4: Entity Extraction (LLM)
A dedicated extraction call receives the combined tagged content (~24k char cap) with strict JSON output rules. Search snippets from Stage 2 are prepended to each page's content — these often contain structured data (ratings, review counts, prices) that JavaScript-rendered pages lose during text conversion. The `source_url` and `source_title` fields are sourced verbatim from the content headers — this is the source attribution mechanism. The prompt instructs the LLM to extract numeric data from any textual format (e.g., "4.6 stars" → 4.6, "$$$" → 3) and to never skip entities due to partial data.

### Stage 5: Enrichment & Ranking (pure JS, no LLM)
Post-processing with quality-aware scoring:
1. **Deduplication** by normalized entity name
2. **Tag normalization** (string → array for tag-type columns)
3. **Quality-aware confidence scoring** — instead of naive field-completeness, the enricher auto-detects quality signal columns from the schema:
   - **Rating columns** (`rating`, `score`): normalized to 0–1 on a 5-point scale
   - **Popularity columns** (`review_count`, `funding`, `github_stars`, `users`): log-normalized
   - **Stars columns**: disambiguated by value (≤5 treated as rating, >5 as popularity)
   - Composite score = 35% field completeness + 65% quality signals (when quality data exists)
   - **Penalization**: when the schema expects quality signals but an entity has none, the score is capped at `completeness × 0.5` — preventing data-sparse entities from reaching "high" confidence
   - Falls back to pure completeness only for schemas with no numeric quality columns
4. **Sorting** by composite score (descending)

This approach is fully domain-agnostic — the same code ranks pizza restaurants by rating+reviews, startups by funding, and software by GitHub stars, without any query type detection or domain-specific logic.

---

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| ~5k char per-page cap | Deeply-nested page data may be truncated | Sufficient for entity extraction in practice |
| ~24k char total content cap | Only ~5-6 pages of full content reach the extractor | Chunked extraction (future work) |
| Ratings/reviews may differ from live data | Scraped text may reflect cached or outdated values | Search snippets included as secondary signal |
| Entity dedup by name only | Spelling variants create duplicates | Fuzzy match (future work) |
| No caching | Every query re-runs full pipeline | Redis cache by query hash (future) |
| API key in browser (standalone) | Not for production | Use backend mode with server-side key |
| Location is manual | User must type their location | Browser Geolocation API (future) |

## Cost & Latency

| Mode | LLM Calls | Search | Typical Time | Est. Cost |
|------|-----------|--------|--------------|-----------|
| Browser (Claude web_search) | 3 | Claude tool | 30-50s | ~$0.03-0.08 |
| With Serper.dev | 2 | Serper (free) | 15-30s | ~$0.02-0.05 |
| Original architecture | 4 | Claude tool | 60-170s | ~$0.05-0.12 |
