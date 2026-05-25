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
#   PART 7 — fuzzy_match whitespace-collapse fallback (RayServe ↔ Ray Serve)
# ════════════════════════════════════════════════════════════════════════════

hr("PART 7 — fuzzy_match: whitespace-collapsed equality")

# Direct cases the fallback should catch
assert fuzzy_match("rayserve", "ray serve"), "RayServe vs Ray Serve"
assert fuzzy_match("pgvector", "pg vector"), "pgvector vs pg vector"
assert fuzzy_match("openai", "open ai"),     "openai vs open ai"
# Substring branch still catches the easy cases unchanged
assert fuzzy_match("antonios", "antonios pizza")
# Token-overlap branch still works
assert fuzzy_match("primo too pizzeria amherst", "primo pizzeria too")
# No false-positives across genuinely different names
assert not fuzzy_match("rayserve", "tritonserve"), "Different names: must not match"
assert not fuzzy_match("pgvector", "milvus"),      "Different names: must not match"

print("  rayserve ↔ ray serve:   match (whitespace fallback)")
print("  pgvector ↔ pg vector:   match (whitespace fallback)")
print("  openai   ↔ open ai:     match (whitespace fallback)")
print("  rayserve ↔ tritonserve: NO match (different names)")

# End-to-end: feed Ray Serve / RayServe as two raw records, confirm they merge
cols_demo = [
    {"key": "name", "type": "text"},
    {"key": "description", "type": "text"},
    {"key": "source_url", "type": "text"},
]
raw_rayserve = [
    {"name": "Ray Serve",  "description": "Distributed serving framework", "source_url": "https://a.com/1"},
    {"name": "RayServe",   "description": "Ray's model server",            "source_url": "https://b.com/2"},
]
merged_rs = merge_entities(raw_rayserve, cols_demo)
assert len(merged_rs) == 1, f"Ray Serve + RayServe should merge into 1, got {len(merged_rs)}"
print(f"  Ray Serve + RayServe merged into 1 canonical entity: '{merged_rs[0]['name']}'")
print(f"  _sourceUrls captured both: {len(merged_rs[0]['_sourceUrls'])} URLs")
print("  PART 7: OK — whitespace fallback catches the Ray Serve/RayServe class")


# ════════════════════════════════════════════════════════════════════════════
#   PART 8 — Scoring now uses domain count (not URL count)
# ════════════════════════════════════════════════════════════════════════════
#
# Two entities, same completeness, same lack of quality data (fallback
# branch). Entity A has 3 URLs all on aussieai.com (1 domain). Entity B
# has 3 URLs across 3 different blogs (3 domains).
#
# Before the change: both score 0.5*comp + 0.5*min(3/3, 1) = 0.5 + 0.5 = 1.0.
# After the change:  A scores 0.5*comp + 0.5*min(1/3, 1) = 0.5 + 0.167 = 0.667;
#                    B scores 0.5*comp + 0.5*min(3/3, 1) = 0.5 + 0.5   = 1.0.
# Plus B's diversity bonus (+0.10 for 2 extra domains) pushes it higher still.

hr("PART 8 — Score uses domain count, not URL count")

schema_d = {"columns": [
    {"key": "name", "type": "text"},
    {"key": "description", "type": "text"},
    {"key": "source_url", "type": "text"},
]}

ent_same_domain = {
    "name": "FrameworkA",
    "description": "A description",
    "source_url": "https://aussieai.com/p1",
    "_sourceUrls": {
        "https://aussieai.com/p1",
        "https://aussieai.com/p2",
        "https://aussieai.com/p3",
    },
}
ent_multi_domain = {
    "name": "FrameworkB",
    "description": "B description",
    "source_url": "https://blog1.com/x",
    "_sourceUrls": {
        "https://blog1.com/x",
        "https://blog2.com/y",
        "https://blog3.com/z",
    },
}

scored_d = score_entities([dict(ent_same_domain), dict(ent_multi_domain)], schema_d)
a_out, b_out = scored_d[0], scored_d[1]

# Manual expected for A (1 domain): 0.5*1.0 + 0.5*min(1/3, 1) + diversity 0 = 0.667
exp_a = 0.5 * 1.0 + 0.5 * (1/3) + 0.0
# Manual expected for B (3 domains): 0.5*1.0 + 0.5*min(3/3, 1) + diversity 0.10 = 1.10
exp_b = 0.5 * 1.0 + 0.5 * 1.0     + (2 * 0.05)

print(f"  A (3 URLs, 1 domain):  _score={a_out['_score']:.4f}   expected≈{exp_a:.4f}   _sources={a_out['_sources']}")
print(f"  B (3 URLs, 3 domains): _score={b_out['_score']:.4f}   expected≈{exp_b:.4f}   _sources={b_out['_sources']}")

# Both retain _sources = 3 URLs (URL count unchanged, only the score formula changed)
assert a_out["_sources"] == 3
assert b_out["_sources"] == 3
# But scores diverge by ~0.43 — the multi-domain entity wins decisively
assert abs(a_out["_score"] - round(exp_a * 100) / 100) < 0.011
assert abs(b_out["_score"] - round(exp_b * 100) / 100) < 0.011
assert b_out["_score"] > a_out["_score"] + 0.30, (
    "Multi-domain entity should outscore same-domain entity by ≥ 0.30. "
    f"Got A={a_out['_score']}, B={b_out['_score']}"
)
print(f"  Δ = {b_out['_score'] - a_out['_score']:.2f}  ← multi-domain wins decisively")
print("  PART 8: OK — listicle-monoculture entities no longer ride on URL count")


# ════════════════════════════════════════════════════════════════════════════
#   PART 9 — Per-domain source cap (search_web_node policy)
# ════════════════════════════════════════════════════════════════════════════

hr("PART 9 — cap_per_domain: drops URLs beyond N per host")

from app.lib.url import cap_per_domain, domain_from_url

# Subdomain normalization smoke test
assert domain_from_url("https://www.aussieai.com/x") == "aussieai.com"
assert domain_from_url("https://m.yelp.com/biz/y") == "yelp.com"
print("  domain_from_url normalization (www/m/mobile): OK")

# Simulated Serper output: 16 aussieai pages, 5 medium pages, 2 unique
fake = [
    *[{"url": f"https://aussieai.com/p{i}",                  "rank": i} for i in range(16)],
    *[{"url": f"https://medium.com/@author/post{i}",         "rank": i} for i in range(5)],
    {"url": "https://github.com/dangkhoasdc/awesome",         "rank": 99},
    {"url": "https://arxiv.org/abs/1234.5678",                "rank": 100},
]
kept, dropped = cap_per_domain(fake, url_getter=lambda x: x["url"], max_per_domain=3)

domains_in_kept = [domain_from_url(x["url"]) for x in kept]
from collections import Counter as _C
counts = _C(domains_in_kept)
print(f"  Input  : 23 URLs across 4 domains "
      f"(16× aussieai.com, 5× medium.com, 1× github.com, 1× arxiv.org)")
print(f"  Output : {len(kept)} URLs, distribution: {dict(counts)}")
print(f"  Dropped: {dropped}")

assert counts["aussieai.com"] == 3, "aussieai.com should be capped to 3"
assert counts["medium.com"]   == 3, "medium.com should be capped to 3"
assert counts["github.com"]   == 1, "github.com under cap, kept as-is"
assert counts["arxiv.org"]    == 1, "arxiv.org under cap, kept as-is"
assert dropped == {"aussieai.com": 13, "medium.com": 2}, f"unexpected dropped: {dropped}"
# Order preservation: the FIRST three aussieai URLs are kept (top-ranked)
kept_aussie_ranks = [x["rank"] for x in kept if "aussieai" in x["url"]]
assert kept_aussie_ranks == [0, 1, 2], f"Should keep top-ranked: got {kept_aussie_ranks}"
print(f"  Top-ranked URLs preserved per domain (ranks {kept_aussie_ranks} for aussieai)")
print("  PART 9: OK — cap drops surplus URLs while keeping top-ranked + under-cap domains intact")


# ════════════════════════════════════════════════════════════════════════════
#   PART 10 — GitHub enrichment (mocked GitHub API)
# ════════════════════════════════════════════════════════════════════════════

hr("PART 10 — enrich_with_github: gating, direct match, search, non-overwrite")

import asyncio
import httpx

from app.lib.github_enrich import enrich_with_github


def _mock_handler(repo_fixtures: dict, search_fixtures: dict | None = None):
    """Build a httpx.MockTransport handler from {(owner, repo): payload} maps.

    ``repo_fixtures`` maps slug → dict (or ``"404"`` for not-found / ``"403"``
    for rate-limit). ``search_fixtures`` maps query → list of repo dicts.
    """
    def handler(request: httpx.Request) -> httpx.Response:

        path = request.url.path
        if path.startswith("/repos/"):
            _, _, owner, repo = path.split("/", 3)
            payload = repo_fixtures.get((owner, repo))
            if payload == "404":
                return httpx.Response(404, json={"message": "Not Found"})
            if payload == "403":
                return httpx.Response(403, json={"message": "rate limited"})
            if payload is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=payload)

        if path == "/search/repositories":
            q = request.url.params.get("q", "")
            items = (search_fixtures or {}).get(q, [])
            return httpx.Response(200, json={"items": items})

        return httpx.Response(404)
    return handler


SCHEMA_WITH_STARS = [
    {"key": "name", "type": "text"},
    {"key": "description", "type": "text"},
    {"key": "source_url", "type": "text"},
    {"key": "github_stars", "type": "number"},
    {"key": "license", "type": "text"},
    {"key": "primary_language", "type": "text"},
]
SCHEMA_NO_STARS = [
    {"key": "name", "type": "text"},
    {"key": "description", "type": "text"},
    {"key": "source_url", "type": "text"},
    {"key": "cuisine", "type": "tags"},
    {"key": "rating", "type": "number"},
]


# ── Gating 1: schema lacks github_stars → no API calls ───────────────────────
result = asyncio.run(enrich_with_github(
    [{"name": "Antonio's Pizza", "source_url": "https://yelp.com/a"}],
    SCHEMA_NO_STARS,
    token="ghp_fake_token",
))
print(f"  Gating (no github_stars col): {result}")
assert result == {"skipped_reason": "no_github_stars_column"}

# ── Unauthenticated mode: token=None still runs (degraded rate limit) ──────
async def run_unauth():
    transport = httpx.MockTransport(_mock_handler({
        ("vllm-project", "vllm"): {
            "stargazers_count": 25400, "license": {"spdx_id": "Apache-2.0"}, "language": "Python",
        },
    }))
    async with httpx.AsyncClient(transport=transport) as ac:
        return await enrich_with_github(
            [{"name": "vllm", "source_url": "https://github.com/vllm-project/vllm",
              "github_stars": None, "license": None, "primary_language": None}],
            SCHEMA_WITH_STARS,
            token=None, client=ac,
        )

result = asyncio.run(run_unauth())
print(f"  Unauth (no token):           {result}")
assert result["enriched"] == 1
assert result["authenticated"] is False, "Stats should expose auth status"

print("  Schema gate + token-optional auth: OK")


# ── Direct URL match: source_url is github.com/owner/repo ───────────────────
fixtures_direct = {
    ("vllm-project", "vllm"): {
        "stargazers_count": 25400,
        "license": {"spdx_id": "Apache-2.0"},
        "language": "Python",
    },
}
ent_direct = {
    "name": "vllm",
    "source_url": "https://github.com/vllm-project/vllm",
    "github_stars": None, "license": None, "primary_language": None,
}

async def run_direct():
    transport = httpx.MockTransport(_mock_handler(fixtures_direct))
    async with httpx.AsyncClient(transport=transport) as ac:
        return await enrich_with_github(
            [ent_direct], SCHEMA_WITH_STARS,
            token="ghp_fake", client=ac,
        )

result = asyncio.run(run_direct())
print(f"\n  Direct URL match: {result}")
print(f"    {ent_direct}")
assert result["enriched"] == 1
assert ent_direct["github_stars"] == 25400
assert ent_direct["license"] == "Apache-2.0"
assert ent_direct["primary_language"] == "Python"
print("  Direct match path: OK")


# ── Search fallback: non-github source_url, name matches a real repo ────────
fixtures_search = {
    ("ggerganov", "llama.cpp"): {
        "stargazers_count": 67000,
        "license": {"spdx_id": "MIT"},
        "language": "C++",
    },
}
search_fixtures = {
    "llama.cpp in:name": [
        {"name": "llama.cpp", "owner": {"login": "ggerganov"},
         "stargazers_count": 67000, "license": {"spdx_id": "MIT"}, "language": "C++"},
        {"name": "llama-cpp-python", "owner": {"login": "abetlen"},
         "stargazers_count": 8500, "license": {"spdx_id": "MIT"}, "language": "Python"},
    ],
}
ent_search = {
    "name": "llama.cpp",
    "source_url": "https://aussieai.com/research/frameworks",   # NOT a github URL
    "github_stars": None, "license": None, "primary_language": None,
}

async def run_search():
    transport = httpx.MockTransport(_mock_handler(fixtures_search, search_fixtures))
    async with httpx.AsyncClient(transport=transport) as ac:
        return await enrich_with_github(
            [ent_search], SCHEMA_WITH_STARS,
            token="ghp_fake", client=ac,
        )

result = asyncio.run(run_search())
print(f"\n  Search fallback: {result}")
print(f"    {ent_search}")
assert result["enriched"] == 1
assert ent_search["github_stars"] == 67000   # from the TOP-star exact-name match
assert ent_search["license"] == "MIT"
assert ent_search["primary_language"] == "C++"
print("  Search-by-name path (top-star exact match): OK")


# ── Non-overwrite: existing values must not be replaced ─────────────────────
ent_existing = {
    "name": "vllm",
    "source_url": "https://github.com/vllm-project/vllm",
    "github_stars": 12345,        # already set — should NOT be overwritten
    "license": "Apache-2.0",      # already set
    "primary_language": None,     # null — should fill
}

async def run_nonoverwrite():
    transport = httpx.MockTransport(_mock_handler({
        ("vllm-project", "vllm"): {
            "stargazers_count": 99999,
            "license": {"spdx_id": "MIT"},
            "language": "Python",
        },
    }))
    async with httpx.AsyncClient(transport=transport) as ac:
        return await enrich_with_github(
            [ent_existing], SCHEMA_WITH_STARS,
            token="ghp_fake", client=ac,
        )

asyncio.run(run_nonoverwrite())
print(f"\n  Non-overwrite test: {ent_existing}")
assert ent_existing["github_stars"] == 12345,      "Must not overwrite existing stars"
assert ent_existing["license"] == "Apache-2.0",    "Must not overwrite existing license"
assert ent_existing["primary_language"] == "Python", "Must fill null primary_language"
print("  Extractor-provided values preserved; only nulls filled: OK")


# ── Search-name strictness: refuses inexact matches ─────────────────────────
# Entity "TGI" should NOT pick up the popular text-generation-inference repo
# because the repo's NAME is "text-generation-inference", not "tgi".
fixtures_inexact_search = {
    "TGI in:name": [
        {"name": "text-generation-inference", "owner": {"login": "huggingface"},
         "stargazers_count": 9500, "license": {"spdx_id": "Apache-2.0"}, "language": "Python"},
        {"name": "tgi-go",  "owner": {"login": "randomuser"},
         "stargazers_count": 12, "license": {"spdx_id": "MIT"}, "language": "Go"},
    ],
}
ent_tgi = {
    "name": "TGI",
    "source_url": "https://aussieai.com/research/frameworks",
    "github_stars": None, "license": None, "primary_language": None,
}

async def run_inexact():
    transport = httpx.MockTransport(_mock_handler({}, fixtures_inexact_search))
    async with httpx.AsyncClient(transport=transport) as ac:
        return await enrich_with_github(
            [ent_tgi], SCHEMA_WITH_STARS,
            token="ghp_fake", client=ac,
        )

result = asyncio.run(run_inexact())
print(f"\n  Inexact-name guard ('TGI' should NOT pick up text-generation-inference):")
print(f"    {ent_tgi}    stats={result}")
assert ent_tgi["github_stars"] is None,  "Inexact match must NOT fill stars"
assert ent_tgi["license"] is None
assert ent_tgi["primary_language"] is None
assert result["errors"] == 1,            "Inexact match must count as an error/skip"
print("  Conservative name matching prevents false positives: OK")


# ── Graceful degradation: rate-limit / 404 — never raises ───────────────────
ents_mixed = [
    {"name": "vllm",        "source_url": "https://github.com/vllm-project/vllm",
     "github_stars": None, "license": None, "primary_language": None},
    {"name": "made-up-repo","source_url": "https://github.com/nobody/made-up-repo",
     "github_stars": None, "license": None, "primary_language": None},
    {"name": "rate-limited","source_url": "https://github.com/limited/rate-limited",
     "github_stars": None, "license": None, "primary_language": None},
]

fixtures_mixed = {
    ("vllm-project", "vllm"): {
        "stargazers_count": 25400, "license": {"spdx_id": "Apache-2.0"}, "language": "Python",
    },
    ("nobody", "made-up-repo"): "404",
    ("limited", "rate-limited"): "403",
}

async def run_mixed():
    transport = httpx.MockTransport(_mock_handler(fixtures_mixed))
    async with httpx.AsyncClient(transport=transport) as ac:
        return await enrich_with_github(
            ents_mixed, SCHEMA_WITH_STARS,
            token="ghp_fake", client=ac,
        )

result = asyncio.run(run_mixed())
print(f"\n  Mixed run (1 hit, 1 404, 1 rate-limit): {result}")
print(f"    vllm:        stars={ents_mixed[0]['github_stars']}")
print(f"    made-up:     stars={ents_mixed[1]['github_stars']}  (404 — skipped)")
print(f"    rate-limit:  stars={ents_mixed[2]['github_stars']}  (403 — skipped)")
assert result["looked_up"] == 3
assert result["enriched"]  == 1
assert result["errors"]    == 2
assert ents_mixed[0]["github_stars"] == 25400
assert ents_mixed[1]["github_stars"] is None
assert ents_mixed[2]["github_stars"] is None
print("  Per-entity HTTP errors are non-fatal; partial enrichment succeeds: OK")


# ── NOASSERTION license guard ───────────────────────────────────────────────
ent_noassert = {
    "name": "unlicensed", "source_url": "https://github.com/x/unlicensed",
    "github_stars": None, "license": None, "primary_language": None,
}

async def run_noassert():
    transport = httpx.MockTransport(_mock_handler({
        ("x", "unlicensed"): {
            "stargazers_count": 42, "license": {"spdx_id": "NOASSERTION"}, "language": "Go",
        },
    }))
    async with httpx.AsyncClient(transport=transport) as ac:
        return await enrich_with_github(
            [ent_noassert], SCHEMA_WITH_STARS,
            token="ghp_fake", client=ac,
        )

asyncio.run(run_noassert())
print(f"\n  NOASSERTION license guard: {ent_noassert}")
assert ent_noassert["github_stars"] == 42
assert ent_noassert["license"] is None,  "NOASSERTION should NOT be propagated as a license"
assert ent_noassert["primary_language"] == "Go"
print("  GitHub's 'NOASSERTION' (no LICENSE file) skipped, stars/lang still filled: OK")


print("  PART 10: OK — GitHub enrichment behaves correctly across all paths")


# ════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 70)
print("  ALL VERIFICATION GROUPS PASSED")
print("═" * 70)
