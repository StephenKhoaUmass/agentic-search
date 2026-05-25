"""Best-effort JSON extraction from LLM text output.

Mirrors ``extractJSON`` in ``frontend/src/lib/agent.js``: LLMs sometimes wrap
JSON in markdown fences, prefix it with prose, or emit a top-level array
inside an otherwise valid object. We try increasingly tolerant strategies
and return ``fallback`` if all fail.
"""

from __future__ import annotations

import json
import re
from typing import Any


_FENCE_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)
_ARRAY_RE = re.compile(r"\[[\s\S]*\]")
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def extract_json(text: str | None, fallback: Any = None) -> Any:
    """Parse ``text`` as JSON with progressive fallbacks.

    Strategy order:
      1. Strip markdown fences, parse the whole string.
      2. Find the longest ``[...]`` substring and parse that.
      3. Find the longest ``{...}`` substring and parse that.
      4. Return ``fallback``.
    """
    if not text:
        return fallback

    cleaned = _FENCE_RE.sub("", text).strip()
    cleaned = cleaned.rstrip("`").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    arr = _ARRAY_RE.search(cleaned)
    if arr is not None:
        try:
            return json.loads(arr.group(0))
        except json.JSONDecodeError:
            pass

    obj = _OBJECT_RE.search(cleaned)
    if obj is not None:
        try:
            return json.loads(obj.group(0))
        except json.JSONDecodeError:
            pass

    return fallback
