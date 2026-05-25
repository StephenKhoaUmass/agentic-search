"""Stage 5 — Enrichment: fuzzy merge → Places cross-walk → GitHub enrich → scoring → filter.

Pure post-processing — no LLM. The node composes four primitives, each in
its own module so they can be unit-tested in isolation:

    1. :func:`fuzzy_merge.merge_entities`
       Collapse per-source records into canonical entities by fuzzy name
       match, then aggregate per-column (median for ratings, max for counts,
       longest string for descriptions, union for tags).

    2. :func:`places.cross_reference_places`
       Overlay authoritative Google Places values onto merged entities
       (prefer rating, max review count, fill address/phone/price). No-op
       when ``places_ref`` is empty (non-local queries).

    3. :func:`github_enrich.enrich_with_github`
       Fill ``github_stars`` / ``license`` / ``primary_language`` from the
       GitHub REST API. Gated on schema having a ``github_stars`` column +
       ``GITHUB_PERSONAL_ACCESS_TOKEN`` being set; no-op otherwise. Must
       run BEFORE :func:`scoring.score_entities` so the freshly-populated
       quality fields flow into the adaptive scoring's
       ``any_entity_has_quality`` global check.

    4. :func:`scoring.score_entities` + :func:`scoring.filter_and_sort`
       Auto-detect quality-signal columns, apply the adaptive composite
       score with the global ``any_entity_has_quality`` check, drop stub
       entities, and sort by score descending.

All four primitives are async-or-pure; this node is just the glue plus
SSE step emission.
"""

from __future__ import annotations

from ...config import get_settings
from ...lib.fuzzy_merge import merge_entities
from ...lib.github_enrich import enrich_with_github
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

        # Step 3a: cross-walk Places values (in-place; no-op when places_ref is [])
        cross_reference_places(merged, columns, places_ref)

        # Step 3b: GitHub enrichment — fills github_stars/license/primary_language
        # from the GitHub REST API. Self-gated on schema + token presence; the
        # node doesn't need to know whether the schema is software/startup/local
        # vertical because the gate lives inside enrich_with_github.
        # Runs BEFORE score_entities so the new quality values participate
        # in the adaptive scoring decision (any_entity_has_quality global check).
        settings = get_settings()
        gh_stats = await enrich_with_github(
            merged, columns,
            token=settings.github_token,
        )

        # Step 4+5: classify quality columns + composite score with adaptive penalty
        scored = score_entities(merged, schema)

        # Step 6+7: filter (<2 filled fields) + sort by score desc
        final = filter_and_sort(scored, columns)

        gh_note = ""
        if gh_stats.get("skipped_reason"):
            gh_note = f" · GitHub: skipped ({gh_stats['skipped_reason']})"
        elif gh_stats.get("looked_up"):
            gh_note = (
                f" · GitHub: {gh_stats['enriched']}/{gh_stats['looked_up']} enriched"
                + (f" · {gh_stats['errors']} errors" if gh_stats.get("errors") else "")
            )

        meta = (
            f"{len(raw_entities)} raw → {len(merged)} merged → {len(final)} kept"
            + (f" · {len(places_ref)} Places refs applied" if places_ref else "")
            + gh_note
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
