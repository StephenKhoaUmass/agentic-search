"""Shared URL helpers.

Tiny module so the same domain-extraction logic is used by both
:mod:`scoring` (per-entity diversity bonus + source-count weighting) and
:mod:`graph.nodes.search_web` (per-domain source cap). Keeping them in
sync via a shared import means we can't accidentally treat
``m.example.com`` and ``www.example.com`` as different domains in one
place and the same in another.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Callable, TypeVar
from urllib.parse import urlparse


_WWW_PREFIX = re.compile(r"^(www|m|mobile)\.")
T = TypeVar("T")


def domain_from_url(url: str) -> str:
    """Return the bare hostname for ``url``, stripping common subdomain
    prefixes (``www``, ``m``, ``mobile``).

    Falls back to ``url`` verbatim when parsing fails — better to treat
    unparseable URLs as unique strings than to collapse them all into
    the empty key.
    """
    try:
        host = urlparse(url).hostname or ""
        return _WWW_PREFIX.sub("", host) if host else url
    except Exception:
        return url


def cap_per_domain(
    items: list[T],
    *,
    url_getter: Callable[[T], str],
    max_per_domain: int,
) -> tuple[list[T], dict[str, int]]:
    """Drop items beyond ``max_per_domain`` per host.

    Preserves input order so the *highest-ranked* item per domain wins
    (the search backend returns results sorted by relevance). Returns
    ``(kept_items, dropped_counts_per_domain)``.
    """
    seen: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    kept: list[T] = []
    for item in items:
        d = domain_from_url(url_getter(item))
        if seen[d] >= max_per_domain:
            dropped[d] += 1
            continue
        seen[d] += 1
        kept.append(item)
    return kept, dict(dropped)
