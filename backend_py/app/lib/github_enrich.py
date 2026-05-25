"""GitHub repository enrichment for entities with a ``github_stars`` schema column.

Fills ``github_stars``, ``license``, and ``primary_language`` from the GitHub
REST API for entities that resolve to a ``github.com`` repository. Resolution
runs two passes per entity (cheapest first):

    1. **Direct URL match.** If ``source_url`` or any URL in
       ``_sourceUrls`` is a ``github.com/{owner}/{repo}`` link, use that
       slug directly — single ``GET /repos/{owner}/{repo}`` (5,000/hr
       authenticated).
    2. **Name search.** Otherwise hit ``GET /search/repositories`` for the
       entity name, take the top star-ranked hit whose repo name matches
       the entity name after lowercase + strip-non-alphanumeric. Costs one
       search credit (30 req/min authenticated).

Design notes
------------
* **MCP-compatible interface.** This module exposes a single async function
  (``enrich_with_github``) that takes a list of entity dicts and mutates
  them in place. To swap to a GitHub MCP server later, replace the bodies
  of ``_fetch_repo`` and ``_search_by_name`` with ``call_claude(mcp_servers=
  [github_mcp])`` calls without touching the node or the scoring code.
* **Why direct REST today**: lower latency (no extra LLM round-trip),
  deterministic resolution (no LLM-driven tool-call decisions), and the
  same authentication surface (``GITHUB_PERSONAL_ACCESS_TOKEN``) that the
  MCP server would consume.
* **Never overwrites a non-null value** — the extractor's text wins. Only
  fills fields the LLM couldn't pull from page content.

Failure modes — all non-fatal, never raise:
* No ``github_stars`` column in schema → return ``{skipped_reason: ...}``.
* No GitHub token → still runs, but **unauthenticated** (60 core req/hr
  and 10 search req/min). Enough for a single demo query (≤10 entities);
  more queries in the same hour will start hitting 403/429 and those
  entities will just stay un-enriched. Set
  ``GITHUB_PERSONAL_ACCESS_TOKEN`` to get 5,000 core / 30 search per
  hour, which removes rate-limit risk for any realistic demo load.
* HTTP 403/429/500/timeout for a single entity → that entity is skipped,
  the rest still get enriched, ``errors`` counter goes up.
* Conservative name matching → only fills when the top-star repo's name
  exactly matches the entity name (case-insensitive, alphanumerics only).
  Avoids assigning star counts from coincidentally-named repos.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx


# Matches ``github.com/owner/repo[/...]`` and captures owner + repo.
_GITHUB_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/?#]+?)(?:\.git)?(?:[/?#].*)?$",
    re.I,
)

# Schema gate: any column key containing ``github_stars`` or exactly ``stars``.
# Matches the existing scoring.py classification of "stars" as a quality signal.
_GITHUB_STARS_COL_RE = re.compile(r"github_stars|^stars$", re.I)

# github.com paths whose first segment is NOT a user/org.
_NON_REPO_OWNERS = frozenset({
    "topics", "search", "marketplace", "explore", "settings",
    "notifications", "sponsors", "trending", "collections", "issues",
    "pulls", "discussions", "stars",
})

# Per-call HTTP timeout for GitHub API requests.
_GITHUB_HTTP_TIMEOUT = 10.0


def _has_github_stars_column(schema_columns: list[dict]) -> bool:
    return any(_GITHUB_STARS_COL_RE.search(c.get("key", "")) for c in schema_columns)


def _extract_slug_from_url(url: str) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` if ``url`` is a real GitHub repo link, else None."""
    m = _GITHUB_URL_RE.match(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if owner.lower() in _NON_REPO_OWNERS:
        return None
    return owner, repo


def _candidate_urls(entity: dict) -> list[str]:
    """All URLs we know about for ``entity``, in priority order."""
    urls: list[str] = []
    s = entity.get("source_url")
    if isinstance(s, str) and s:
        urls.append(s)
    for u in entity.get("_sourceUrls") or []:
        if isinstance(u, str) and u and u not in urls:
            urls.append(u)
    return urls


def _normalize_name(name: str) -> str:
    """Lowercase + drop all non-alphanumerics. ``llama.cpp`` → ``llamacpp``."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _is_null_or_empty(v: Any) -> bool:
    return v is None or v == "" or v == "null"


async def _fetch_repo(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    headers: dict,
) -> dict | None:
    """``GET /repos/{owner}/{repo}``. Returns ``None`` on any failure."""
    try:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers,
            timeout=_GITHUB_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


async def _search_by_name(
    client: httpx.AsyncClient,
    name: str,
    headers: dict,
) -> dict | None:
    """``GET /search/repositories`` — top star-ranked exact-name match, or None.

    The name match is intentionally strict (normalized equality) to avoid
    pulling stats from a coincidentally-named repo. E.g., entity ``"TGI"``
    won't match the popular ``text-generation-inference`` repo because the
    repo's name is ``"text-generation-inference"``, not ``"TGI"`` — better
    to leave the field null than to assign wrong data.
    """
    norm_query = _normalize_name(name)
    if not norm_query:
        return None
    try:
        resp = await client.get(
            "https://api.github.com/search/repositories",
            headers=headers,
            params={
                "q": f"{name} in:name",
                "sort": "stars",
                "order": "desc",
                "per_page": 5,
            },
            timeout=_GITHUB_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        items = (resp.json().get("items") or [])
    except (httpx.HTTPError, ValueError):
        return None

    for item in items:
        if _normalize_name(item.get("name") or "") == norm_query:
            return item
    return None


async def _resolve_and_fetch(
    client: httpx.AsyncClient,
    entity: dict,
    headers: dict,
) -> dict | None:
    """Direct URL match first, then fall back to a name search."""
    for url in _candidate_urls(entity):
        slug = _extract_slug_from_url(url)
        if slug is None:
            continue
        data = await _fetch_repo(client, slug[0], slug[1], headers)
        if data is not None:
            return data

    name = entity.get("name") or ""
    if not name.strip():
        return None
    return await _search_by_name(client, name, headers)


def _apply_repo_data(entity: dict, repo: dict) -> bool:
    """Fill null fields on ``entity`` from ``repo``. Returns True if anything changed."""
    changed = False

    stars = repo.get("stargazers_count")
    if _is_null_or_empty(entity.get("github_stars")) and isinstance(stars, int):
        entity["github_stars"] = stars
        changed = True

    if _is_null_or_empty(entity.get("license")):
        spdx = (repo.get("license") or {}).get("spdx_id")
        # GitHub returns "NOASSERTION" when a repo has no LICENSE file — skip it.
        if spdx and spdx != "NOASSERTION":
            entity["license"] = spdx
            changed = True

    if _is_null_or_empty(entity.get("primary_language")):
        lang = repo.get("language")
        if lang:
            entity["primary_language"] = lang
            changed = True

    return changed


async def enrich_with_github(
    entities: list[dict],
    schema_columns: list[dict],
    *,
    token: str | None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """In-place GitHub stats enrichment for entities with a ``github_stars`` column.

    Args:
        entities: list of merged entity dicts. Mutated in place.
        schema_columns: planner schema columns; gates the call.
        token: GitHub personal access token. Optional — unauthenticated
            mode runs at 60 core / 10 search req/hr, which is fine for a
            single demo query but will throttle under any heavier load.
        client: optional ``httpx.AsyncClient`` for dependency injection in
            tests. Defaults to a fresh client.

    Returns:
        Stats dict:
          * ``looked_up``: entities we attempted to resolve
          * ``enriched``: entities where ≥1 field was filled
          * ``errors``:   entities where the GitHub API call returned ``None``
            (rate-limited, 404, or network error)
          * ``authenticated``: whether we sent an ``Authorization`` header
          * ``skipped_reason``: present only when the entire call was skipped
    """
    if not _has_github_stars_column(schema_columns):
        return {"skipped_reason": "no_github_stars_column"}
    if not entities:
        return {"looked_up": 0, "enriched": 0, "errors": 0, "authenticated": bool(token)}

    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        results = await asyncio.gather(*(
            _resolve_and_fetch(client, e, headers) for e in entities
        ))
    finally:
        if owns_client:
            await client.aclose()

    enriched = errors = 0
    for entity, repo_data in zip(entities, results):
        if repo_data is None:
            errors += 1
            continue
        if _apply_repo_data(entity, repo_data):
            enriched += 1

    return {
        "looked_up": len(entities),
        "enriched": enriched,
        "errors": errors,
        "authenticated": bool(token),
    }
