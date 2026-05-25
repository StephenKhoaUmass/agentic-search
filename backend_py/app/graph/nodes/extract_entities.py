"""Stage 4 — Structured Entity Extraction via Claude.

Translates ``extractEntities`` from ``agent.js``. Single Claude call at
``temperature=0`` for deterministic output. Each entity-source occurrence
becomes a SEPARATE record (e.g. "PostgreSQL" mentioned on Yelp and on a
blog produces two records) — cross-source validation happens later in
``enrich_entities_node``.

Strict qualifier rules are baked into the system prompt: e.g. "startups"
excludes large corporations, "open source" excludes proprietary tools.
This is the single biggest lever for output relevance.
"""

from __future__ import annotations

import json
from typing import Any

from ...config import get_settings
from ...lib.claude import call_claude
from ...lib.json_utils import extract_json
from ...streaming.events import emit_done, emit_error, emit_running
from ..state import PipelineState


def _build_example_object(columns: list[dict]) -> dict[str, Any]:
    """Build the JSON skeleton shown to the LLM as a per-entity template."""
    obj: dict[str, Any] = {}
    for c in columns:
        t = c.get("type", "text")
        if t == "tags":
            obj[c["key"]] = ["tag1", "tag2"]
        elif t == "number":
            obj[c["key"]] = 0
        else:
            obj[c["key"]] = "value or null"
    obj["source_url"] = "https://..."
    obj["source_title"] = "Page Title"
    return obj


def _build_system_prompt(schema: dict) -> str:
    cols_desc = "\n".join(
        f"  - {c['key']} ({c.get('type', 'text')}): {c.get('description', '')}"
        for c in schema.get("columns", [])
    )
    example = _build_example_object(schema.get("columns", []))

    return f"""You are a precision data extraction agent. {schema.get('extraction_prompt', '')}

Extract entities from the content and output ONLY a valid JSON array (no markdown, no explanation):
[{json.dumps(example, indent=2)}]

Column definitions:
{cols_desc}

STRICT rules:
- Extract ALL distinct entities mentioned across ALL source documents — be thorough
- IMPORTANT: distribute extraction across EVERY source, not just the first few. Each source should contribute entities
- CRITICAL: only extract entities that are DIRECTLY about the query topic and match ALL qualifiers:
  - "AI startups in healthcare" → only healthcare startups. Skip large corporations (Google Health, Microsoft), non-healthcare companies, and entities mentioned only as cross-industry comparisons
  - "open source database tools" → only open-source tools. Skip proprietary/commercial-only tools
  - If an entity is mentioned as a comparison, analogy, or contrast to the main topic (e.g., "unlike Company X in legal tech"), do NOT extract it
- When the same entity appears in multiple sources, create a SEPARATE record for each source occurrence (each with its own source_url). This enables cross-source validation — duplicates will be merged later
- For number fields: extract from any textual format (e.g., "4.6 stars" → 4.6, "1,523 reviews" → 1523, "$$$" → 3, "$25-35" → 30, "47.2k stars" → 47200)
- Use null ONLY when a field's data is genuinely not present — do NOT leave a field null if the source mentions it in any format
- NEVER invent or hallucinate values not supported by the source text
- source_url MUST be copied verbatim from a "=== SOURCE: url ===" header
- source_title MUST match the "Title:" field under that source header
- type "tags" → JSON array of strings
- type "url" → full https:// URL or null
- Do not skip entities just because some fields are missing — partial data is valuable"""


def _build_tagged_content(pages: list[dict], cap: int) -> str:
    """Concatenate pages into a single content blob with ``=== SOURCE: url ===``
    headers between them, then cap total length to ``cap`` chars."""
    blocks = []
    for p in pages:
        b = f"=== SOURCE: {p['url']} ===\nTitle: {p.get('title', '')}"
        if p.get("snippet"):
            b += f"\nSnippet: {p['snippet']}"
        b += f"\n{p.get('content', '')}"
        blocks.append(b)
    return "\n\n".join(blocks)[:cap]


async def extract_entities_node(state: PipelineState) -> dict:
    started = await emit_running("extract", "Extracting structured entities…")

    try:
        schema = state["schema"]
        pages = state.get("pages") or []
        query = state["query"]

        settings = get_settings()
        system_prompt = _build_system_prompt(schema)
        tagged = _build_tagged_content(pages, settings.max_content_chars)

        text = await call_claude(
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": (
                    f'Query: "{query}"\n\nSource Content:\n{tagged}\n\n'
                    f'Extract {schema.get("entity_type", "entities")}:'
                ),
            }],
            max_tokens=settings.extract_max_tokens,
            temperature=0,
        )

        entities = extract_json(text, [])
        if not isinstance(entities, list):
            snippet = (text or "<empty>")[:200]
            raise ValueError(f"Extractor returned non-array output: {snippet!r}")

        done = await emit_done(
            "extract",
            f"Extracted {len(entities)} raw records",
            started,
            meta="merging & scoring…",
        )

        return {"raw_entities": entities, "step_log": [done]}

    except Exception as e:
        await emit_error("extract", f"Extraction failed: {e}", started)
        raise
