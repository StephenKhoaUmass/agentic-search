"""Serper Places API integration and column-mapping config.

This module is the single source of truth for two things:

1. ``PLACES_COL_MAP`` — declarative mapping from schema column-key regex
   patterns to Google Places fields, with a merge mode per row. Used by:
     * :func:`has_mappable_columns` (here) to decide whether a Places API
       call is worth making for a given schema.
     * ``enrich_entities_node`` (later) to cross-walk Places values onto
       merged entities at enrichment time.
   Ported verbatim from ``frontend/src/lib/agent.js`` to keep the two
   pipelines behaviorally consistent.

2. :func:`fetch_serper_places` — async POST to /places. Single call per
   request (1 Serper credit). Returns ``[]`` on transient failure so the
   pipeline degrades to extraction-only data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from .fuzzy_merge import fuzzy_match, normalize_name


PlacesMergeMode = Literal["prefer", "max", "fill"]


@dataclass(frozen=True, slots=True)
class PlacesColumnMapping:
    """One row in PLACES_COL_MAP."""

    pattern: re.Pattern[str]
    field: str
    mode: PlacesMergeMode


# Keep in sync with frontend/src/lib/agent.js PLACES_COL_MAP.
PLACES_COL_MAP: tuple[PlacesColumnMapping, ...] = (
    PlacesColumnMapping(re.compile(r"^rating$|^score$", re.I),                          "rating",      "prefer"),
    PlacesColumnMapping(re.compile(r"review.*(count|num)|num.*review|^reviews$", re.I), "ratingCount", "max"),
    PlacesColumnMapping(re.compile(r"^address$|^location$", re.I),                      "address",     "fill"),
    PlacesColumnMapping(re.compile(r"phone", re.I),                                     "phoneNumber", "fill"),
    PlacesColumnMapping(re.compile(r"price", re.I),                                     "priceLevel",  "fill"),
)


def has_mappable_columns(schema: dict) -> bool:
    """True iff the schema has at least one column ``PLACES_COL_MAP`` can map.

    Gates the Places API call in ``search_web_node`` — for queries like
    "open source vector databases" no schema column matches any of these
    patterns, so we skip the call entirely.
    """
    for col in schema.get("columns", []):
        key = col.get("key", "")
        for m in PLACES_COL_MAP:
            if m.pattern.search(key):
                return True
    return False


async def fetch_serper_places(
    *,
    query: str,
    location: str | None,
    api_key: str,
    timeout_seconds: float = 30.0,
) -> list[dict]:
    """Single Google Places call via Serper. Returns raw place dicts or ``[]``.

    Uses the raw user query (with ``near me`` → location substitution) rather
    than schema-generated search queries, for stable reproducible results.
    """
    q = query
    if location:
        q = re.sub(r"\bnear me\b|\bnearby\b", location, q, flags=re.I)

    body: dict = {"q": q}
    if location:
        body["location"] = location

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(
                "https://google.serper.dev/places",
                json=body,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            )
            if not resp.is_success:
                return []
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    places = data.get("places") or []
    return places if isinstance(places, list) else []


# ─── Step 3: cross-walk Places values onto merged entities ───────────────────


def _positive_float(v: Any) -> float | None:
    try:
        n = float(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def cross_reference_places(
    merged_entities: list[dict],
    schema_columns: list[dict],
    places_ref: list[dict],
) -> None:
    """In-place: overlay authoritative Google Places values onto entities
    according to :data:`PLACES_COL_MAP` rules.

    Merge modes:
      * ``"prefer"`` — always take the Places value (e.g. rating: Google's
        value is backed by orders of magnitude more reviews than scraped
        sources).
      * ``"max"`` — take whichever is higher (e.g. review_count).
      * ``"fill"`` — only overwrite null/empty (e.g. address, phone, price).

    Matches the JS ``// Step 3: Cross-reference with Places data`` block in
    ``enrichEntities``.
    """
    if not places_ref or not merged_entities:
        return

    # Pre-compute which schema column maps to which Places field+mode.
    # One mapping per column (the first PLACES_COL_MAP match wins).
    mappings: list[tuple[str, str, str]] = []
    for col in schema_columns:
        key = col.get("key", "")
        for m in PLACES_COL_MAP:
            if m.pattern.search(key):
                mappings.append((key, m.field, m.mode))
                break

    if not mappings:
        return

    for entity in merged_entities:
        ent_name = normalize_name(entity.get("name"))
        if not ent_name:
            continue

        # First Places hit whose title fuzzy-matches this entity.
        match: dict | None = None
        for p in places_ref:
            place_name = normalize_name(p.get("title"))
            if place_name and fuzzy_match(ent_name, place_name):
                match = p
                break
        if match is None:
            continue

        for col_key, place_field, mode in mappings:
            place_val = match.get(place_field)
            if place_val is None or place_val == "":
                continue

            cur_val = entity.get(col_key)
            cur_empty = cur_val is None or cur_val == "" or cur_val == "null"

            if mode == "fill" and cur_empty:
                entity[col_key] = place_val
            elif mode == "max":
                pv = _positive_float(place_val)
                if pv is None:
                    continue
                cv = _positive_float(cur_val) if not cur_empty else None
                if cv is None or pv > cv:
                    entity[col_key] = pv
            elif mode == "prefer":
                pv = _positive_float(place_val)
                if pv is not None:
                    entity[col_key] = pv
