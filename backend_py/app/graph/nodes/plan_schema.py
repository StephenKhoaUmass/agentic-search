"""Stage 1 — Schema Planner.

Translates ``planSchema`` from ``frontend/src/lib/agent.js`` with two
extensions specific to this backend's AI/ML & startup focus:

1. **Domain priors.** When the query is about AI/ML tools or frameworks the
   schema is required to include ``github_stars``, ``license``, and
   ``primary_language``. When it's about startups it's required to include
   ``funding_stage``, ``total_funding``, and ``founded_year``. These are the
   columns the downstream enrichment / scoring code expects for quality
   signals — leaving them out would silently degrade ranking quality.

2. **Source priors.** Search queries 2-3 are nudged toward GitHub Awesome
   lists, curated AI directories, arXiv, and academic surveys. This biases
   source diversity toward authoritative domains for the AI/ML vertical
   without introducing domain-specific code paths in later stages.

The node runs exactly once per request. The retry loop short-circuits past
it (``reformulate_queries → search_web``), so we never re-plan inside an
iteration.

Failure mode: if the LLM returns unparseable JSON or output that lacks the
required top-level keys, the node raises ``ValueError`` and LangGraph
propagates the failure up to the FastAPI endpoint. We do NOT silently fall
back to a default schema — a malformed schema would corrupt every
downstream node.
"""

from __future__ import annotations

from ...config import get_settings
from ...lib.claude import call_claude
from ...lib.json_utils import extract_json
from ...streaming.events import emit_done, emit_error, emit_running
from ..state import PipelineState


_SYSTEM_PROMPT = """You are an expert data schema designer specialized in AI/ML tools, frameworks, libraries, and startup intelligence. Given a search query, design an optimal extraction schema.

Output ONLY valid JSON (no markdown, no backticks, no explanation):
{
  "entity_type": "concise description of what entities we are finding",
  "columns": [
    { "key": "name", "label": "Name", "type": "text", "description": "primary name of the entity" },
    { "key": "...", "label": "...", "type": "text|url|tags|number", "description": "..." }
  ],
  "search_queries": ["query 1", "query 2", "query 3", "query 4"],
  "extraction_prompt": "2-3 sentence instructions for extracting these entities accurately"
}

Rules:
- ALWAYS include these first 3 columns: name, description, source_url
- Add 3-6 domain-specific columns. ALWAYS include quantitative metrics when available.

Domain priors (apply the closest match to the query intent):
  - **AI/ML tools, frameworks, libraries, open-source projects** → MUST include all of:
      github_stars (number),
      license (text, e.g. MIT, Apache 2.0, GPL, BSD),
      primary_language (text, e.g. Python, Rust, TypeScript, C++)
    Useful extras: category, website, model_support (tags), last_release
  - **Startups, companies, AI ventures** → MUST include all of:
      funding_stage (text, e.g. Pre-seed, Seed, Series A, Series B, Series C+),
      total_funding (number, in USD — convert "$50M" to 50000000),
      founded_year (number)
    Useful extras: headquarters, employee_count, focus_area, website
  - **People** (researchers, founders) → role, organization, expertise, location, social_url
  - **Restaurants/food** → cuisine, price_range, rating (number), review_count (number), address, phone

- Column keys: snake_case. type "tags" = array of strings (e.g. technologies, categories).
- search_queries: EXACTLY 4 queries, each targeting a DIFFERENT source type for maximum diversity:
  1. A broad aggregator/listicle query (e.g. "top X in Y 2025", "best X for Y")
  2. A structured data / curated-list query. For AI/ML tools and frameworks, PRIORITIZE:
     - GitHub Awesome lists ("awesome-X github", "site:github.com topic:X")
     - Curated AI directories (e.g. "site:huggingface.co X", "site:paperswithcode.com X")
     - Comparison tables ("X comparison chart", "X vs Y vs Z")
  3. A specific/niche query from a different angle. For AI/ML topics, INCLUDE arXiv or
     academic surveys (e.g. "site:arxiv.org X survey", "X benchmark paper").
     For other topics: community forums, industry reports, alternative perspectives.
  4. A curated/directory query (e.g. "X funded by Y Combinator", "X directory list",
     "site:producthunt.com X", "site:crunchbase.com X")
  Goal: each query should find DIFFERENT domains. Avoid queries that all return the same blog posts.

- extraction_prompt: 2-3 sentence instructions covering:
  1. What qualifies as an entity (be STRICT: if the query says "startups", exclude established
     corporations; if it says "open source", exclude proprietary tools)
  2. How to handle the domain-specific fields"""


_REQUIRED_KEYS = ("entity_type", "columns", "search_queries", "extraction_prompt")


async def plan_schema_node(state: PipelineState) -> dict:
    started = await emit_running("plan", "Planning extraction schema…")

    try:
        query = state["query"]
        location = state.get("location")

        location_ctx = (
            f'\nUser location: {location}. Replace "near me", "nearby", or '
            f'location-relative phrases in search queries with "{location}".'
            if location else ""
        )

        settings = get_settings()
        text = await call_claude(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f'Query: "{query}"{location_ctx}'}],
            max_tokens=settings.schema_max_tokens,
        )

        schema = extract_json(text)
        if not isinstance(schema, dict) or not all(k in schema for k in _REQUIRED_KEYS):
            snippet = (text or "<empty>")[:200]
            raise ValueError(
                f"Schema planner returned unparseable or incomplete output. "
                f"First 200 chars: {snippet!r}"
            )

        if not isinstance(schema["search_queries"], list) or not schema["search_queries"]:
            raise ValueError("Schema planner returned no search_queries.")
        if not isinstance(schema["columns"], list) or not schema["columns"]:
            raise ValueError("Schema planner returned no columns.")

        preview = " | ".join(str(q) for q in schema["search_queries"][:2])
        done = await emit_done(
            "plan",
            f"Schema ready: {schema['entity_type']} · {len(schema['columns'])} columns",
            started,
            meta=f"Queries: {preview}",
        )

        return {
            "schema": schema,
            # First-iteration query set, recorded so reformulate_queries_node
            # can dedupe future proposals against it.
            "reformulated_queries": [list(schema["search_queries"])],
            "step_log": [done],
        }

    except Exception as e:
        await emit_error("plan", f"Schema planning failed: {e}", started)
        raise
