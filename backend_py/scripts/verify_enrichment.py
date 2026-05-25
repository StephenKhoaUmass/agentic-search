"""Numeric verification of fuzzy_merge + scoring + places cross-walk.

Run from `backend_py/`:
    python -m scripts.verify_enrichment

Prints each entity's score with the formula breakdown so the math can be
eyeballed against agent.js. The three critical scoring branches are
exercised in three separate test groups, A / B / C.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.lib.fuzzy_merge import fuzzy_match, merge_entities, normalize_name, stem_token
from app.lib.places import cross_reference_places
from app.lib.scoring import (
    classify_quality_columns,
    compute_completeness,
    compute_quality,
    filter_and_sort,
    score_entities,
)


def hr(title: str) -> None:
    print(f"\n{'═' * 70}\n  {title}\n{'═' * 70}")


# ════════════════════════════════════════════════════════════════════════════
#   PART 1 — fuzzy_merge primitives
# ════════════════════════════════════════════════════════════════════════════

hr("PART 1 — normalize_name / stem_token / fuzzy_match")

# normalize_name
assert normalize_name("Antonio's Pizza") == "antonios pizza"
assert normalize_name("  PINECONE  ") == "pinecone"
assert normalize_name("Hugging  Face\u2019s") == "hugging faces"
assert normalize_name(None) is None
assert normalize_name("null") is None
assert normalize_name("") is None
print("  normalize_name: OK")

# stem_token
assert stem_token("databases") == "database"
assert stem_token("startups") == "startup"
assert stem_token("is") == "is"            # too short, untouched
assert stem_token("css") == "css"          # exactly 3 chars, untouched
assert stem_token("kubernetes") == "kubernete"  # known limitation, matches JS
print("  stem_token: OK")

# fuzzy_match — three tiers
assert fuzzy_match("antonios pizza", "antonios pizza")              # exact
assert fuzzy_match("antonios", "antonios pizza")                    # substring
assert fuzzy_match("primo too pizzeria amherst", "primo pizzeria too")  # token-overlap
assert not fuzzy_match("antonios pizza", "domino's")
assert not fuzzy_match("pizza place", "burger joint")
# Single-overlap fails (needs ≥ 2)
assert not fuzzy_match("primo too pizzeria", "primo something else entirely")
print("  fuzzy_match: OK")


# ════════════════════════════════════════════════════════════════════════════
#   PART 2 — merge_entities
# ════════════════════════════════════════════════════════════════════════════

hr("PART 2 — merge_entities (Step 1+2)")

schema_pizza_columns = [
    {"key": "name", "type": "text"},
    {"key": "description", "type": "text"},
    {"key": "source_url", "type": "text"},
    {"key": "cuisine", "type": "tags"},
    {"key": "rating", "type": "number"},
    {"key": "review_count", "type": "number"},
    {"key": "address", "type": "text"},
    {"key": "phone", "type": "text"},
]

raw_pizza = [
    {
        "name": "Antonio's Pizza",
        "description": "Short desc",
        "source_url": "https://yelp.com/biz/antonios",
        "cuisine": "Pizza",
        "rating": 4.5,
        "review_count": 369,
        "address": "",
        "phone": None,
        "source_title": "Yelp",
    },
    {
        "name": "Antonio's Pizza By The Slice",   # fuzzy-matches → same group
        "description": "Longer, more detailed description here",
        "source_url": "https://tripadvisor.com/biz/antonios",
        "cuisine": ["Pizza", "Italian"],
        "rating": 4.6,
        "review_count": 1500,
        "address": "31 N Pleasant St",
        "phone": "(413) 253-0808",
        "source_title": "TripAdvisor",
    },
    {
        "name": "Domino's Pizza",
        "description": "Chain",
        "source_url": "https://dominos.com",
        "cuisine": "Pizza",
        "rating": 3.0,
        "review_count": 50,
        "address": None,
        "phone": None,
        "source_title": "Site",
    },
]

merged = merge_entities(raw_pizza, schema_pizza_columns)
print(f"  Merged {len(raw_pizza)} raw → {len(merged)} canonical entities")

ant = next(e for e in merged if "antonio" in e["name"].lower())
dom = next(e for e in merged if "domino" in e["name"].lower())

# Canonical name = longest variant
assert ant["name"] == "Antonio's Pizza By The Slice", ant["name"]
# Rating = median of [4.5, 4.6]; with 2 values, JS uses index floor(2/2)=1 → 4.6
assert ant["rating"] == 4.6, ant["rating"]
# review_count = max of [369, 1500] → 1500
assert ant["review_count"] == 1500
# description = longest
assert ant["description"] == "Longer, more detailed description here"
# tags = union of "Pizza" + ["Pizza", "Italian"]
assert set(ant["cuisine"]) == {"Pizza", "Italian"}, ant["cuisine"]
# address = filled value (only one record had it)
assert ant["address"] == "31 N Pleasant St"
# _sourceUrls captures all contributing URLs
assert len(ant["_sourceUrls"]) == 2

# Domino's is its own group (didn't fuzzy-match Antonio's)
assert dom["rating"] == 3.0
assert len(dom["_sourceUrls"]) == 1
print("  Antonio's merge: name, rating-median, count-max, description-longest, tag-union, address-fill: OK")
print(f"  ant._sourceUrls = {ant['_sourceUrls']}")


# ════════════════════════════════════════════════════════════════════════════
#   PART 3 — adaptive scoring (the high-risk port)
# ════════════════════════════════════════════════════════════════════════════
#
# Three groups exercise the three branches of the composite score:
#   A) Schema HAS quality cols, SOME entities have data → mixed (reward + penalize)
#   B) Schema HAS quality cols, NO entity has data       → FALLBACK
#   C) Schema has NO quality cols                        → FALLBACK
#
# ════════════════════════════════════════════════════════════════════════════

hr("PART 3A — any_entity_has_quality=True, entity has data  vs  no data")

schema_A = {
    "columns": [
        {"key": "name", "type": "text"},
        {"key": "description", "type": "text"},
        {"key": "source_url", "type": "text"},
        {"key": "rating", "type": "number"},
        {"key": "review_count", "type": "number"},
    ]
}

rating_cols, pop_cols, stars_cols = classify_quality_columns(schema_A["columns"])
print(f"  rating_cols={[c['key'] for c in rating_cols]}")
print(f"  popularity_cols={[c['key'] for c in pop_cols]}")
print(f"  stars_cols={[c['key'] for c in stars_cols]}")
assert [c["key"] for c in rating_cols] == ["rating"]
assert [c["key"] for c in pop_cols] == ["review_count"]
assert stars_cols == []
print("  classify_quality_columns: OK")

# Two entities — one with full quality, one with none. Identical completeness.
e_with_quality = {
    "name": "Antonio's",
    "description": "Pizza place",
    "source_url": "https://yelp.com/a",
    "rating": 4.6,
    "review_count": 1500,
    "_sourceUrls": {"https://yelp.com/a", "https://tripadvisor.com/a"},  # 2 unique domains
}
e_without_quality = {
    "name": "Some Other Place",
    "description": "Filler description",
    "source_url": "https://example.com/b",
    "rating": None,
    "review_count": None,
    "_sourceUrls": {"https://example.com/b", "https://blog.com/b"},  # 2 unique domains
}

scored_A = score_entities([dict(e_with_quality), dict(e_without_quality)], schema_A)
withQ, withoutQ = scored_A[0], scored_A[1]

# ── Manual expected math for entity WITH quality data ──────────────────────
# domain_cols = description, rating, review_count   (3 cols)
# filled       = 3                                  → completeness = 3/3 = 1.0
# rating term  : weight 1, value min(4.6/5, 1)      = 0.92
# popcount term: weight 2, value min(log10(1501)/4, 1) ≈ min(3.176/4, 1) = 0.794
# quality_score = (1*0.92 + 2*0.794) / 3            = (0.92 + 1.588)/3   ≈ 0.836
# source_count = 2 → min(2/3, 1) = 0.667
# branch: any_entity_has_quality=True, has_quality_data=True
#   → 0.15*1.0 + 0.55*0.836 + 0.30*0.667           ≈ 0.15 + 0.4598 + 0.2  = 0.8098
# diversity_bonus: 2 domains → (2-1)*0.05 = 0.05
# final ≈ 0.8598 → rounded 0.86, confidence high
pop_term  = min(math.log10(1501) / 4, 1)
qual_A    = (1 * 0.92 + 2 * pop_term) / 3
expected  = 0.15 * 1.0 + 0.55 * qual_A + 0.30 * (2/3) + 0.05

print(f"  WITH quality:    _score={withQ['_score']:.4f}   expected≈{expected:.4f}   (Δ={abs(withQ['_score'] - expected):.4f})")
print(f"                   _confidence={withQ['_confidence']}")
print(f"                   completeness=1.0  quality_score≈{qual_A:.4f}  sources=2  domains=2")
assert abs(withQ["_score"] - round(expected * 100) / 100) < 0.011
assert withQ["_confidence"] == "high"

# ── Manual expected math for entity WITHOUT quality data ───────────────────
# completeness = 1/3 ≈ 0.333  (description filled; rating/review_count null)
# branch: any_entity_has_quality=True, has_quality_data=False → PENALTY
#   → completeness * 0.35 = 0.333 * 0.35 ≈ 0.1167
# diversity_bonus: 2 domains → 0.05
# final ≈ 0.1667 → rounded 0.17, confidence low
expected_nq = (1/3) * 0.35 + 0.05
print(f"  WITHOUT quality: _score={withoutQ['_score']:.4f}   expected≈{expected_nq:.4f}   (Δ={abs(withoutQ['_score'] - expected_nq):.4f})")
print(f"                   _confidence={withoutQ['_confidence']}")
print(f"                   completeness≈0.333 → penalized (any_entity_has_quality=True)")
assert abs(withoutQ["_score"] - round(expected_nq * 100) / 100) < 0.011
assert withoutQ["_confidence"] == "low"
print("  PART 3A: OK — penalty branch fires correctly")


hr("PART 3B — any_entity_has_quality=False (schema HAS quality cols, no data)")

# Same schema as A, but BOTH entities have null rating + null review_count.
e1 = {
    "name": "Startup One",
    "description": "Description A",
    "source_url": "https://crunchbase.com/a",
    "rating": None,
    "review_count": None,
    "_sourceUrls": {"https://crunchbase.com/a", "https://techcrunch.com/a"},
}
e2 = {
    "name": "Startup Two",
    "description": "Description B",
    "source_url": "https://crunchbase.com/b",
    "rating": None,
    "review_count": None,
    "_sourceUrls": {"https://crunchbase.com/b"},
}

scored_B = score_entities([dict(e1), dict(e2)], schema_A)
b1, b2 = scored_B

# Fallback branch: 0.5 * completeness + 0.5 * min(source_count/3, 1) + diversity
# e1: completeness = 1/3, source_count=2 → 0.5*(1/3) + 0.5*(2/3) + 0.05 = 0.5333
# e2: completeness = 1/3, source_count=1 → 0.5*(1/3) + 0.5*(1/3) + 0    = 0.3333
exp_b1 = 0.5 * (1/3) + 0.5 * (2/3) + 0.05
exp_b2 = 0.5 * (1/3) + 0.5 * (1/3) + 0.0
print(f"  e1 (2 sources):  _score={b1['_score']:.4f}   expected≈{exp_b1:.4f}   confidence={b1['_confidence']}")
print(f"  e2 (1 source):   _score={b2['_score']:.4f}   expected≈{exp_b2:.4f}   confidence={b2['_confidence']}")
assert abs(b1["_score"] - round(exp_b1 * 100) / 100) < 0.011
assert abs(b2["_score"] - round(exp_b2 * 100) / 100) < 0.011
# Critically: NOT penalized to completeness*0.35 = 0.117. Got the fallback formula.
assert b1["_score"] > 0.4, "e1 should NOT be penalized (fallback branch)"
print("  PART 3B: OK — fallback branch fires (no penalty when nobody has quality data)")


hr("PART 3C — schema has NO quality columns at all")

schema_C = {
    "columns": [
        {"key": "name", "type": "text"},
        {"key": "description", "type": "text"},
        {"key": "github_url", "type": "text"},
        {"key": "source_url", "type": "text"},
    ]
}
e_c = {
    "name": "Lib",
    "description": "A library",
    "github_url": "https://github.com/lib/lib",
    "source_url": "https://example.com",
    "_sourceUrls": {"https://example.com"},
}

scored_C = score_entities([dict(e_c)], schema_C)
c1 = scored_C[0]
# domain_cols excludes name + source_url → [description, github_url] = 2 cols, both filled
# → completeness = 2/2 = 1.0; source_count = 1 → min(1/3, 1) = 0.333
# fallback formula: 0.5*1.0 + 0.5*0.333 = 0.667
expected_c = 0.5 * 1.0 + 0.5 * (1/3)
print(f"  no-quality-cols: _score={c1['_score']:.4f}   expected≈{expected_c:.4f}   confidence={c1['_confidence']}")
assert abs(c1["_score"] - round(expected_c * 100) / 100) < 0.011
print("  PART 3C: OK — no quality cols → fallback branch")


# ════════════════════════════════════════════════════════════════════════════
#   PART 4 — stars column dual semantics (≤5 → rating, >5 → popularity)
# ════════════════════════════════════════════════════════════════════════════

hr("PART 4 — stars column: ≤5 treated as rating, >5 as popularity")

schema_stars = {
    "columns": [
        {"key": "name", "type": "text"},
        {"key": "source_url", "type": "text"},
        {"key": "stars", "type": "number"},
    ]
}

_, _, stars_cols = classify_quality_columns(schema_stars["columns"])
assert [c["key"] for c in stars_cols] == ["stars"]
print(f"  stars classified as stars_cols: OK")

# v=4.5 → rating: weight 1, value 4.5/5 = 0.9
e_rating = {"name": "RepoA", "source_url": "https://github.com/a", "stars": 4.5,
            "_sourceUrls": {"https://github.com/a"}}
q1, has1 = compute_quality(e_rating, [], [], stars_cols)
print(f"  stars=4.5 → quality={q1:.4f}  expected=0.9000  has_data={has1}")
assert abs(q1 - 0.9) < 1e-9

# v=25000 → popularity: weight 2, value min(log10(25001)/4, 1) ≈ 1.0
e_pop = {"name": "RepoB", "source_url": "https://github.com/b", "stars": 25000,
         "_sourceUrls": {"https://github.com/b"}}
q2, has2 = compute_quality(e_pop, [], [], stars_cols)
expected_q2 = min(math.log10(25001) / 4, 1)
print(f"  stars=25000 → quality={q2:.4f}  expected={expected_q2:.4f}  has_data={has2}")
assert abs(q2 - expected_q2) < 1e-9

# Combined: 4.5 (weight 1, val 0.9) AND 25000 (weight 2, val ~1.0)
# But that's two records of same entity, not realistic; skip combined case.
print("  PART 4: OK — stars dual semantics correct")


# ════════════════════════════════════════════════════════════════════════════
#   PART 5 — Places cross-walk (prefer / max / fill)
# ════════════════════════════════════════════════════════════════════════════

hr("PART 5 — cross_reference_places: prefer / max / fill modes")

cols_places = [
    {"key": "name", "type": "text"},
    {"key": "rating", "type": "number"},        # → prefer
    {"key": "review_count", "type": "number"},  # → max
    {"key": "address", "type": "text"},         # → fill
    {"key": "phone", "type": "text"},           # → fill (won't overwrite)
    {"key": "price_range", "type": "text"},     # → fill
    {"key": "source_url", "type": "text"},
]

entities_p = [{
    "name": "Antonio's Pizza By The Slice",
    "rating": 4.5,            # Places=4.6 → prefer should overwrite to 4.6
    "review_count": 369,      # Places=1500 → max should overwrite to 1500
    "address": "",            # empty → fill should set it
    "phone": "(413) 555-0000",  # already set → fill should NOT overwrite
    "price_range": None,      # null → fill should set it
    "source_url": "https://yelp.com",
}]

places_p = [{
    "title": "Antonio's Pizza By The Slice",
    "rating": 4.6,
    "ratingCount": 1500,
    "address": "31 N Pleasant St, Amherst, MA",
    "phoneNumber": "(413) 253-0808",
    "priceLevel": "$",
}]

cross_reference_places(entities_p, cols_places, places_p)
out = entities_p[0]
print(f"  rating       4.5 → {out['rating']}            (prefer; expect 4.6)")
print(f"  review_count 369 → {out['review_count']}     (max; expect 1500)")
print(f"  address      '' → {out['address']!r}         (fill; expect Places value)")
print(f"  phone        kept (had value) → {out['phone']!r}   (fill skip; expect (413) 555-0000)")
print(f"  price_range  None → {out['price_range']!r}   (fill; expect '$')")

assert out["rating"] == 4.6
assert out["review_count"] == 1500
assert out["address"] == "31 N Pleasant St, Amherst, MA"
assert out["phone"] == "(413) 555-0000"   # fill mode does NOT overwrite non-empty
assert out["price_range"] == "$"
print("  PART 5: OK — prefer / max / fill modes all correct")

# Max mode: existing value HIGHER than Places → should keep existing
entities_p2 = [{"name": "X", "rating": 4.5, "review_count": 9999, "source_url": ""}]
places_p2   = [{"title": "X", "ratingCount": 100}]
cross_reference_places(entities_p2, cols_places, places_p2)
assert entities_p2[0]["review_count"] == 9999, "max mode should keep the larger value"
print("  PART 5b: OK — max mode keeps existing when it's larger")


# ════════════════════════════════════════════════════════════════════════════
#   PART 6 — filter_and_sort
# ════════════════════════════════════════════════════════════════════════════

hr("PART 6 — filter_and_sort: drops <2 filled, sorts by score desc")

cols = [
    {"key": "name", "type": "text"},
    {"key": "description", "type": "text"},
    {"key": "rating", "type": "number"},
    {"key": "source_url", "type": "text"},
]
entities_f = [
    {"name": "A", "description": "x", "rating": 4.0, "source_url": "u", "_score": 0.3},  # 2 filled → keep
    {"name": "B", "description": "y", "rating": None, "source_url": None, "_score": 0.9},  # 1 filled → drop
    {"name": "C", "description": "z", "rating": 3.0, "source_url": "u", "_score": 0.5},  # 2 filled → keep
]
out = filter_and_sort(entities_f, cols)
print(f"  kept names in order: {[e['name'] for e in out]}")
assert [e["name"] for e in out] == ["C", "A"]
print("  PART 6: OK — B dropped (only 1 filled field), C ranked above A by _score")


# ════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 70)
print("  ALL VERIFICATION GROUPS PASSED")
print("═" * 70)
