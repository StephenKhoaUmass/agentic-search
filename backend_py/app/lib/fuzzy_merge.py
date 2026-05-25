"""Name normalization, fuzzy matching, and per-source record merging.

Direct translation of ``normalizeName`` / ``stemToken`` / ``fuzzyMatch`` from
``agent.js``, plus the Step 1+2 merging logic that collapses per-source
records into canonical entities.

Used by:
  * :func:`merge_entities` in this module (Step 1+2 of enrichment).
  * :func:`places.cross_reference_places` (matching merged entities to
    Google Places hits during Step 3).
"""

from __future__ import annotations

import re
from typing import Any


# Various apostrophe / backtick variants the LLM tends to mix:
#   U+0027 ' apostrophe   U+2018/U+2019 ‘’ curly quotes   U+0060 ` backtick
_APOSTROPHE_RE = re.compile(r"[\u2018\u2019'`]")
_WS_RE = re.compile(r"\s+")


def normalize_name(name: Any) -> str | None:
    """Lowercase + strip apostrophes + collapse whitespace.

    Returns ``None`` for empty inputs and the literal string ``"null"``
    (which LLMs sometimes emit as a string instead of a JSON null).
    """
    s = str(name or "").lower().strip()
    s = _APOSTROPHE_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return None if not s or s == "null" else s


def stem_token(t: str) -> str:
    """Strip trailing 's' from tokens longer than 3 chars.

    Cheap singular/plural normalization for fuzzy matching:
      ``"databases" → "database"``, ``"startups" → "startup"``, ``"is" → "is"``.
    """
    if len(t) > 3 and t.endswith("s"):
        return t[:-1]
    return t


def fuzzy_match(a: str, b: str) -> bool:
    """True if ``a`` and ``b`` plausibly refer to the same entity.

    Three-tier strategy:
      1. Exact match.
      2. Substring (handles ``"Antonio's Pizza" ⊂ "Antonio's Pizza By The Slice"``).
      3. Stemmed-token overlap: requires ≥ 2 shared tokens AND containment ≥ 0.7
         where containment = overlap / size of smaller token set.
         Handles ``"Primo Too Pizzeria Amherst" ↔ "Primo Pizzeria Too"``.
    """
    if a == b:
        return True
    if a in b or b in a:
        return True

    tok_a = {stem_token(t) for t in a.split() if len(t) > 1}
    tok_b = {stem_token(t) for t in b.split() if len(t) > 1}
    overlap = tok_a & tok_b

    if len(overlap) < 2:
        return False

    containment = max(
        len(overlap) / max(len(tok_a), 1),
        len(overlap) / max(len(tok_b), 1),
    )
    return containment >= 0.7


# ─── Step 1+2: Entity merging ────────────────────────────────────────────────


def _coerce_positive_float(v: Any) -> float | None:
    """Return ``v`` as a positive float, else None. Mirrors the JS pattern
    ``Number(v)`` followed by ``!isNaN(n) && n > 0``."""
    try:
        n = float(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def merge_entities(raw_entities: list[dict], schema_columns: list[dict]) -> list[dict]:
    """Group per-source records by fuzzy name, then aggregate per column.

    Aggregation rules (matching agent.js Step 2):
      * column type ``number`` and key matches ``rating|score`` → median.
      * column type ``number`` otherwise → max.
      * column type ``tags`` → union of all tags (string-or-list tolerant).
      * column type anything else → longest value (longest description wins).

    The canonical name is the longest variant seen across the group.

    Returns merged entity dicts with two extra fields:
      * ``_sourceUrls`` — set of contributing source URLs (used by
        scoring for diversity); kept private (leading underscore).
      * ``source_title`` — copied from the first record.
    """
    groups: list[dict[str, Any]] = []

    # ── Step 1: fuzzy-group raw entities by normalized name ─────────────────
    for e in raw_entities:
        name = normalize_name(e.get("name"))
        if name is None:
            continue

        matched = False
        for g in groups:
            if fuzzy_match(name, g["key"]):
                g["records"].append(e)
                # Prefer the longest variant as canonical
                if len(e.get("name") or "") > len(g["canonical"]):
                    g["canonical"] = e["name"]
                if len(name) > len(g["key"]):
                    g["key"] = name
                matched = True
                break

        if not matched:
            groups.append({
                "key": name,
                "canonical": e.get("name") or "",
                "records": [e],
            })

    # ── Step 2: build one merged entity per group ───────────────────────────
    merged: list[dict] = []
    for g in groups:
        records: list[dict] = g["records"]
        source_urls = {r["source_url"] for r in records if r.get("source_url")}
        result: dict[str, Any] = {"name": g["canonical"], "_sourceUrls": source_urls}

        for col in schema_columns:
            key = col["key"]
            if key == "name":
                continue
            if key == "source_url":
                result["source_url"] = records[0].get("source_url")
                continue

            values = [
                r.get(key) for r in records
                if r.get(key) not in (None, "", "null")
            ]
            if not values:
                result[key] = None
                continue

            col_type = col.get("type", "text")
            if col_type == "number":
                nums = [n for v in values if (n := _coerce_positive_float(v)) is not None]
                if not nums:
                    result[key] = None
                elif re.search(r"rating|score", key, re.I):
                    nums.sort()
                    result[key] = nums[len(nums) // 2]   # median (lower-mid)
                else:
                    result[key] = max(nums)
            elif col_type == "tags":
                all_tags: set[str] = set()
                for v in values:
                    if isinstance(v, list):
                        all_tags.update(str(t).strip() for t in v if str(t).strip())
                    elif isinstance(v, str):
                        for t in re.split(r"[,;]+", v):
                            t = t.strip()
                            if t:
                                all_tags.add(t)
                result[key] = list(all_tags)
            else:
                # Longest value wins (typically: longest description string)
                result[key] = max(values, key=lambda v: len(str(v)))

        result["source_title"] = records[0].get("source_title") or ""
        merged.append(result)

    return merged
