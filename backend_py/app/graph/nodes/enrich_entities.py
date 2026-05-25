"""Stage 5 — Enrichment: fuzzy merge → Places cross-walk → quality scoring → filter.

Pure post-processing — no LLM, no network I/O. The node composes three
primitives, each in its own module so they can be unit-tested in isolation:

    1. :func:`fuzzy_merge.merge_entities`
       Collapse per-source records into canonical entities by fuzzy name
       match, then aggregate per-column (median for ratings, max for counts,
       longest string for descriptions, union for tags).

    2. :func:`places.cross_reference_places`
       Overlay authoritative Google Places values onto merged entities
       (prefer rating, max review count, fill address/phone/price). No-op
       when ``places_ref`` is empty (non-local queries).

    3. :func:`scoring.score_entities` + :func:`scoring.filter_and_sort`
       Auto-detect quality-signal columns, apply the adaptive composite
       score with the global ``any_entity_has_quality`` check, drop stub
       entities, and sort by score descending.

All three primitives are pure functions; this node is just the glue plus
SSE step emission.
"""

from __future__ import annotations

from ...lib.fuzzy_merge import merge_entities
from ...lib.places import cross_reference_places
from ...lib.scoring import filter_and_sort, score_entities
from ...streaming.events import emit_done, emit_error, emit_running
from ..state import PipelineState


async def enrich_entities_node(state: PipelineState) -> dict:
    started = await emit_running("enrich", "Merging entities, scoring…")

    try:
        raw_entities = state.get("raw_entities") or []
        schema = state["schema"]
        places_ref = state.get("places_ref") or []
        columns = schema.get("columns", [])

        # Step 1+2: fuzzy-merge raw per-source records into canonical entities
        merged = merge_entities(raw_entities, columns)

        # Step 3: cross-walk Places values (in-place; no-op when places_ref is [])
        cross_reference_places(merged, columns, places_ref)

        # Step 4+5: classify quality columns + composite score with adaptive penalty
        scored = score_entities(merged, schema)

        # Step 6+7: filter (<2 filled fields) + sort by score desc
        final = filter_and_sort(scored, columns)

        meta = (
            f"{len(raw_entities)} raw → {len(merged)} merged → {len(final)} kept"
            + (f" · {len(places_ref)} Places refs applied" if places_ref else "")
        )
        done = await emit_done(
            "enrich",
            f"Enriched to {len(final)} entities",
            started,
            meta=meta,
        )

        return {"entities": final, "step_log": [done]}

    except Exception as e:
        await emit_error("enrich", f"Enrichment failed: {e}", started)
        raise
