"""Stage 7 — Reformulate Search Queries (retry branch only).

Triggered by the ``should_retry`` conditional edge after a failed quality
evaluation. Calls Claude with a small focused prompt to propose 4 NEW
search queries that target different source types and angles than what
was already tried.

Dedupe-failure guard
--------------------
If every proposed query (case-insensitive) is a duplicate of one already in
``reformulated_queries`` history, this node sets ``quality_verdict='fail'``
with ``quality_reason='no_new_queries'``. The ``has_new_queries`` conditional
edge then routes to END instead of looping back into ``search_web`` with
already-tried queries.

State updates on success:
    - ``schema.search_queries`` ← the novel queries
    - ``iteration`` ← current + 1
    - ``reformulated_queries`` ← appended with the novel set
    - ``quality_verdict`` ← "retry" (in flight again)
"""

from __future__ import annotations

from ...lib.claude import call_claude
from ...lib.json_utils import extract_json
from ...streaming.events import emit_done, emit_error, emit_running
from ..state import PipelineState


_SYSTEM = """You are a search query strategist. Given an original user query, a list of search queries already tried (which produced poor results), and the reason they failed, propose 4 NEW search queries that target DIFFERENT source types and angles than the previous attempts.

Output ONLY valid JSON (no markdown, no backticks, no prose):
{ "queries": ["query 1", "query 2", "query 3", "query 4"] }

Rules:
- Each new query must be substantively different from all previously tried queries.
- Target diverse source types: aggregators, structured/curated lists, niche/specialized sources, directories.
- If failure reason is "too_few_sources" or "too_few_entities": broaden, use different keywords, try different domains.
- If failure reason is "low_confidence": pivot to authoritative/curated sources (awesome lists, official directories, curated databases, arXiv, Hugging Face).
- For AI/ML topics, prioritize site:github.com, site:arxiv.org, site:huggingface.co, site:paperswithcode.com.
- For startup topics, prioritize site:crunchbase.com, site:ycombinator.com, site:techcrunch.com.
- Avoid duplicating any previously-tried query verbatim or with only superficial differences."""


def _flatten_history(history: list[list[str]]) -> set[str]:
    """Lowercase-normalized set of every query in any prior set."""
    return {
        q.strip().lower()
        for qset in history
        for q in qset
        if isinstance(q, str) and q.strip()
    }


def _parse_queries(text: str) -> list[str]:
    """Extract queries from Claude's response. Tolerates either
    ``{queries: [...]}`` or a bare array, since LLMs occasionally drop the
    wrapper."""
    parsed = extract_json(text, None)
    if isinstance(parsed, dict):
        qs = parsed.get("queries")
        if isinstance(qs, list):
            return [q for q in qs if isinstance(q, str) and q.strip()]
    if isinstance(parsed, list):
        return [q for q in parsed if isinstance(q, str) and q.strip()]
    return []


async def reformulate_queries_node(state: PipelineState) -> dict:
    started = await emit_running("reformulate", "Reformulating search queries…")

    try:
        query = state["query"]
        history = state.get("reformulated_queries") or []
        reason = state.get("quality_reason") or "low_confidence"
        current_iter = state.get("iteration", 0)

        already_tried = _flatten_history(history)
        history_lines = (
            "\n".join(f"  Iter {i}: {qset}" for i, qset in enumerate(history))
            if history else "  (none)"
        )

        user_msg = (
            f'Original query: "{query}"\n'
            f"Failure reason: {reason}\n"
            f"Previously tried query sets:\n{history_lines}\n\n"
            f"Generate 4 new, diverse search queries."
        )

        text = await call_claude(
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=400,
            # A little creativity for variety, but not full freedom.
            temperature=0.5,
        )

        proposed = _parse_queries(text)
        novel = [q for q in proposed if q.strip().lower() not in already_tried]

        # ── Dedupe-failure guard ────────────────────────────────────────────
        if not novel:
            done = await emit_done(
                "reformulate",
                "No new query angles available — terminating",
                started,
                meta=(
                    f"All {len(proposed)} proposed queries duplicated prior attempts"
                    if proposed else "Claude returned no usable queries"
                ),
            )
            return {
                "quality_verdict": "fail",
                "quality_reason": "no_new_queries",
                "step_log": [done],
            }

        novel = novel[:4]
        new_schema = dict(state["schema"])
        new_schema["search_queries"] = novel

        done = await emit_done(
            "reformulate",
            f"Generated {len(novel)} new queries (iteration {current_iter + 1})",
            started,
            meta=" | ".join(novel[:2]),
        )

        return {
            "schema": new_schema,
            "iteration": current_iter + 1,
            # ``reformulated_queries`` is Annotated[list, add], so this single-
            # element wrapper appends `novel` to history.
            "reformulated_queries": [novel],
            "quality_verdict": "retry",
            "step_log": [done],
        }

    except Exception as e:
        await emit_error("reformulate", f"Reformulation failed: {e}", started)
        raise
