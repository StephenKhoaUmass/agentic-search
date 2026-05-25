"""LangGraph wiring for the agentic search pipeline.

Topology
--------
    START → plan_schema → search_web → scrape_pages → extract_entities → enrich_entities → evaluate_quality
                              ↑                                                                    │
                              │                                                       (conditional: should_retry)
                              │                                                                    │
                              │                                       retry & iter < max ──▶ reformulate_queries
                              │                                                                    │
                              │                                                    (conditional: has_new_queries)
                              │                                                                    │
                              └─────────────────── new_queries ◀───────────────────────────────────┤
                                                                                                   │
                                                                            all_dupes / fail ─▶ END
                                                                            pass / exhausted ──▶ END

Two conditional edges, two terminal exits:
    1. ``evaluate_quality`` — either retry (loop) or end.
    2. ``reformulate_queries`` — either go back to ``search_web`` with new
       queries, or end immediately if the reformulator returned only duplicates
       of previously-tried query sets (it sets ``quality_verdict='fail'`` and
       ``quality_reason='no_new_queries'`` in that case).
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import (
    enrich_entities_node,
    evaluate_quality_node,
    extract_entities_node,
    plan_schema_node,
    reformulate_queries_node,
    scrape_pages_node,
    search_web_node,
)
from .state import PipelineState


# ─── Conditional edge functions ──────────────────────────────────────────────


def should_retry(state: PipelineState) -> str:
    """Route out of ``evaluate_quality``.

    The iteration-budget decision lives in ``evaluate_quality_node``: it sets
    ``verdict=fail`` when the budget is exhausted. This edge just trusts the
    verdict so the rule isn't duplicated in two places.
    """
    if state.get("quality_verdict") == "retry":
        return "reformulate_queries"
    return END


def has_new_queries(state: PipelineState) -> str:
    """Route out of ``reformulate_queries``.

    The reformulator dedupes its proposed queries against
    ``reformulated_queries`` history. If every candidate is a duplicate of a
    previously-tried set, it flips the verdict to ``fail`` with reason
    ``no_new_queries`` and we terminate. Otherwise we loop back into the
    search stage with the freshly-written ``schema.search_queries``.
    """
    if state.get("quality_verdict") == "fail":
        return END
    return "search_web"


# ─── Graph construction ──────────────────────────────────────────────────────


def build_graph():
    """Compile and return the executable pipeline graph."""
    g: StateGraph[PipelineState] = StateGraph(PipelineState)

    # Nodes (names match the ``id`` field used by the React UI for step events)
    g.add_node("plan_schema",         plan_schema_node)
    g.add_node("search_web",          search_web_node)
    g.add_node("scrape_pages",        scrape_pages_node)
    g.add_node("extract_entities",    extract_entities_node)
    g.add_node("enrich_entities",     enrich_entities_node)
    g.add_node("evaluate_quality",    evaluate_quality_node)
    g.add_node("reformulate_queries", reformulate_queries_node)

    # Linear edges (forward path)
    g.add_edge(START, "plan_schema")
    g.add_edge("plan_schema",      "search_web")
    g.add_edge("search_web",       "scrape_pages")
    g.add_edge("scrape_pages",     "extract_entities")
    g.add_edge("extract_entities", "enrich_entities")
    g.add_edge("enrich_entities",  "evaluate_quality")

    # Conditional 1: retry loop entry
    g.add_conditional_edges(
        "evaluate_quality",
        should_retry,
        ["reformulate_queries", END],
    )

    # Conditional 2: loop back vs terminate (dedupe-failure guard)
    g.add_conditional_edges(
        "reformulate_queries",
        has_new_queries,
        ["search_web", END],
    )

    return g.compile()


if __name__ == "__main__":
    # Manual edge verification: print Mermaid for visual inspection.
    print(build_graph().get_graph().draw_mermaid())
