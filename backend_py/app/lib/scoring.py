"""Quality-aware ranking for merged entities (JS Steps 4-7).

Schema-driven — auto-detects which columns carry quality signals (ratings,
popularity counts, GitHub stars) from the schema, then assigns a composite
score per entity.

ADAPTIVE PENALTY — the highest-risk part of the port
----------------------------------------------------
An entity is only penalized for missing quality data if **at least one
other entity in the result set has it**. If NO entity has any quality
data at all (sources just didn't include those fields, common for
startup queries with sparse funding info), we fall back to a
completeness + source-count score so low-data queries return useful
results instead of a near-empty table.

The JS comment, verbatim:

    // Check globally: does ANY entity have quality data?
    // If no entity has quality data, don't penalize — the sources just didn't have it.

This module's :func:`score_entities` preserves that exact branching with
all three composite-score formulas:

    if any_entity_has_quality:
        if has_quality_data:  score = 0.15*c + 0.55*q + 0.30*min(s/3, 1)
        else:                 score = c * 0.35                            # penalized
    else:                     score = 0.5*c  + 0.5*min(s/3, 1)            # fallback

Diversity bonus (``+0.05`` per unique domain beyond the first, capped at
3 domains = ``+0.15``) is added on top in all branches.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .url import domain_from_url


# ─── Column-key patterns ────────────────────────────────────────────────────
# Order matters for stars_cols — must not double-count anything already
# classified as rating or popularity.
_RATING_RE     = re.compile(r"rating|score", re.I)
_POPULARITY_RE = re.compile(r"review|count|votes|popularity|funding|revenue|users|downloads", re.I)
_STARS_RE      = re.compile(r"stars", re.I)

# Confidence bucket thresholds (raw score, NOT rounded _score)
_HIGH_THRESHOLD = 0.5
_MID_THRESHOLD  = 0.25


def classify_quality_columns(
    schema_columns: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Auto-detect quality-signal columns.

    Returns ``(rating_cols, popularity_cols, stars_cols)``. ``stars_cols``
    excludes any column already in rating or popularity to avoid
    double-counting (e.g. a column called ``review_count`` matches
    ``popularity`` first and is removed from ``stars``).
    """
    number_cols = [c for c in schema_columns if c.get("type") == "number"]

    rating_cols     = [c for c in number_cols if _RATING_RE.search(c["key"])]
    popularity_cols = [c for c in number_cols if _POPULARITY_RE.search(c["key"])]
    rating_keys     = {c["key"] for c in rating_cols}
    pop_keys        = {c["key"] for c in popularity_cols}
    stars_cols      = [
        c for c in number_cols
        if _STARS_RE.search(c["key"])
        and c["key"] not in rating_keys
        and c["key"] not in pop_keys
    ]
    return rating_cols, popularity_cols, stars_cols


# ─── Per-entity primitives ──────────────────────────────────────────────────


def _is_filled(v: Any) -> bool:
    if v is None or v == "" or v == "null":
        return False
    if isinstance(v, list) and len(v) == 0:
        return False
    return True


def _positive_float(v: Any) -> float | None:
    try:
        n = float(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _js_round_2(x: float) -> float:
    """JS ``Math.round(x * 100) / 100`` — round half toward +∞.

    Python's ``round`` uses banker's rounding, which can disagree on .5
    boundaries (e.g. ``round(0.5)`` returns ``0`` in Py, ``1`` in JS).
    Use ``floor(x + 0.5)`` so the two pipelines never diverge on display.
    """
    return math.floor(x * 100 + 0.5) / 100


def compute_completeness(entity: dict, schema_columns: list[dict]) -> tuple[float, int]:
    """Fraction of domain (non-``name``, non-``source_url``) columns filled.

    Returns ``(ratio, filled_count)``.
    """
    domain_cols = [c for c in schema_columns if c["key"] not in ("name", "source_url")]
    filled = sum(1 for c in domain_cols if _is_filled(entity.get(c["key"])))
    return filled / max(len(domain_cols), 1), filled


def compute_quality(
    entity: dict,
    rating_cols: list[dict],
    popularity_cols: list[dict],
    stars_cols: list[dict],
) -> tuple[float, bool]:
    """Weighted quality score in ``[0, 1]`` plus a ``has_data`` flag.

    Weights (matching ``agent.js`` verbatim):
      * rating columns:     weight 1, normalize ``min(v/5, 1)``
      * popularity columns: weight 2, normalize ``min(log10(v+1)/4, 1)``
      * stars columns:
          - ``v ≤ 5``: weight 1, treated as a rating → ``v/5``
          - ``v > 5``: weight 2, treated as popularity → ``log10(v+1)/4``

    Popularity is weighted 2× rating because volume of evidence (1,500
    reviews) carries more signal than a single 4.6-star rating.
    """
    weighted_sum = 0.0
    weight_total = 0.0

    for c in rating_cols:
        n = _positive_float(entity.get(c["key"]))
        if n is not None:
            weighted_sum += 1 * min(n / 5, 1)
            weight_total += 1

    for c in popularity_cols:
        n = _positive_float(entity.get(c["key"]))
        if n is not None:
            weighted_sum += 2 * min(math.log10(n + 1) / 4, 1)
            weight_total += 2

    for c in stars_cols:
        n = _positive_float(entity.get(c["key"]))
        if n is not None:
            if n <= 5:
                weighted_sum += 1 * (n / 5)
                weight_total += 1
            else:
                weighted_sum += 2 * min(math.log10(n + 1) / 4, 1)
                weight_total += 2

    score = weighted_sum / weight_total if weight_total > 0 else 0.0
    return score, weight_total > 0


# ─── Step 4+5: scoring orchestrator ─────────────────────────────────────────


def score_entities(merged: list[dict], schema: dict) -> list[dict]:
    """Apply the full scoring pipeline to ``merged`` entities (mutates them).

    Reads ``schema['columns']`` to detect quality-signal columns and
    iterates entities to compute composite scores. The global
    ``any_entity_has_quality`` check controls whether the penalty branch
    fires — see module docstring.
    """
    columns = schema.get("columns", [])
    rating_cols, popularity_cols, stars_cols = classify_quality_columns(columns)
    quality_cols = rating_cols + popularity_cols + stars_cols
    has_quality_signals = len(quality_cols) > 0

    # ── ADAPTIVE PENALTY — global check ─────────────────────────────────────
    # "Check globally: does ANY entity have quality data?
    #  If no entity has quality data, don't penalize."   — agent.js
    any_entity_has_quality = has_quality_signals and any(
        any(_positive_float(e.get(c["key"])) is not None for c in quality_cols)
        for e in merged
    )

    tag_cols = [c for c in columns if c.get("type") == "tags"]

    scored: list[dict] = []
    for e in merged:
        # ── Source diversity bookkeeping ────────────────────────────────────
        # NOTE: the score formula uses DOMAIN count rather than URL count
        # (five aussieai.com pages shouldn't outscore one aussieai.com page +
        # one zenml.io page). URL count is still exposed as `_sources` for
        # display/debug since it doesn't drive ranking anywhere.
        source_urls: set[str] = e.get("_sourceUrls") or set()
        source_count = len(source_urls)
        source_domains = {domain_from_url(u) for u in source_urls}
        domain_count = len(source_domains)
        e.pop("_sourceUrls", None)

        # Normalize string-tag values into lists (per-row safety net)
        for c in tag_cols:
            v = e.get(c["key"])
            if isinstance(v, str):
                e[c["key"]] = [t.strip() for t in re.split(r"[,;]+", v) if t.strip()]

        completeness, _ = compute_completeness(e, columns)
        quality_score, has_quality_data = compute_quality(
            e, rating_cols, popularity_cols, stars_cols,
        )

        # Up to 3 unique domains beyond the first → +0.15 max.
        # This still rewards reaching more domains beyond the saturation
        # point of the source-count term (≥ 3 domains).
        diversity_bonus = min(max(domain_count - 1, 0), 3) * 0.05

        # ── Composite score (mirrors JS structure, but with domain_count) ───
        if any_entity_has_quality:
            if has_quality_data:
                score = (
                    0.15 * completeness
                    + 0.55 * quality_score
                    + 0.30 * min(domain_count / 3, 1)
                )
            else:
                # Penalty branch: pool has quality data, this entity doesn't.
                score = completeness * 0.35
        else:
            # Fallback branch: nobody has quality data; ignore the quality
            # dimension entirely. Equal weight to completeness and domains.
            score = 0.5 * completeness + 0.5 * min(domain_count / 3, 1)
        score += diversity_bonus

        e["_sources"] = source_count
        e["_score"] = _js_round_2(score)
        # Use the raw (un-rounded) score for confidence — matches JS.
        e["_confidence"] = (
            "high" if score >= _HIGH_THRESHOLD
            else "mid" if score >= _MID_THRESHOLD
            else "low"
        )
        scored.append(e)

    return scored


# ─── Step 6+7: filter and sort ──────────────────────────────────────────────


def filter_and_sort(scored: list[dict], schema_columns: list[dict]) -> list[dict]:
    """Drop entities with fewer than 2 filled domain fields, sort by score desc.

    Matches the JS ``filled >= 2`` filter plus the final ``sort((a, b) =>
    b._score - a._score)``.
    """
    domain_cols = [c for c in schema_columns if c["key"] not in ("name", "source_url")]

    kept = [
        e for e in scored
        if sum(1 for c in domain_cols if _is_filled(e.get(c["key"]))) >= 2
    ]
    kept.sort(key=lambda e: e.get("_score", 0), reverse=True)
    return kept
