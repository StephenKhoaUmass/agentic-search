"""Stage 6 — Quality Evaluation (decision node, no LLM, no I/O).

Decides whether the current iteration's results are good enough to terminate
or warrant another reformulation pass. Checks in priority order:

    1. ``too_few_sources``   — len(sources) < 3
    2. ``too_few_entities``  — len(raw_entities) < 5
    3. ``low_confidence``    — >60% of final entities have _confidence == 'low'

Verdict mapping (this node owns the iteration-budget decision; the
``should_retry`` conditional edge just trusts the verdict):

    - All checks pass             → verdict = "pass"
    - Failure AND budget left     → verdict = "retry"
    - Failure AND budget exhausted→ verdict = "fail" (quality_reason preserved)

Budget arithmetic
-----------------
``iteration`` counts completed iterations (starts at 0 after the first
extract/enrich finishes). ``max_iterations`` is the total budget. So:

    iteration + 1 < max_iterations  ⇔  one more iteration would still fit

With the default ``max_iterations=2`` this allows exactly one retry past
the initial run (2 iterations total).
"""

from __future__ import annotations

from ...streaming.events import emit_done, emit_error, emit_running
from ..state import PipelineState


_MIN_SOURCES = 3
_MIN_RAW_ENTITIES = 5
_MAX_LOW_CONFIDENCE_RATIO = 0.6


async def evaluate_quality_node(state: PipelineState) -> dict:
    started = await emit_running("evaluate", "Evaluating result quality…")

    try:
        sources = state.get("sources") or []
        raw_entities = state.get("raw_entities") or []
        entities = state.get("entities") or []
        iteration = state.get("iteration", 0)
        max_iter = state.get("max_iterations", 2)

        # Priority-ordered failure detection
        reason: str | None = None
        if len(sources) < _MIN_SOURCES:
            reason = "too_few_sources"
        elif len(raw_entities) < _MIN_RAW_ENTITIES:
            reason = "too_few_entities"
        elif entities:
            low = sum(1 for e in entities if e.get("_confidence") == "low")
            if low / len(entities) > _MAX_LOW_CONFIDENCE_RATIO:
                reason = "low_confidence"

        # Resolve verdict against the iteration budget
        if reason is None:
            verdict = "pass"
            text = "Quality OK — passing through"
            meta = f"{len(entities)} entities, {len(sources)} sources, iter {iteration}"
        elif iteration + 1 < max_iter:
            verdict = "retry"
            text = "Quality insufficient — will retry"
            meta = f"reason={reason}, iter {iteration}/{max_iter - 1}"
        else:
            verdict = "fail"
            text = "Quality insufficient and retry budget exhausted"
            meta = f"reason={reason}, iter {iteration}/{max_iter - 1} (exhausted)"

        done = await emit_done("evaluate", text, started, meta=meta)

        return {
            "quality_verdict": verdict,
            "quality_reason": reason,
            "step_log": [done],
        }

    except Exception as e:
        await emit_error("evaluate", f"Evaluation failed: {e}", started)
        raise
