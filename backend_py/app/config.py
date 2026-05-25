"""Application configuration.

Loads ``backend_py/.env`` once at import time via python-dotenv and exposes a
frozen ``Settings`` dataclass through a cached ``get_settings()`` accessor.

Required env vars are validated lazily on first call to ``get_settings()`` so
importing this module never fails — useful for unit tests that monkey-patch
the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH, override=False)


def _env(name: str, default: str | None = None) -> str | None:
    """Read an env var, treating empty strings as missing."""
    value = os.environ.get(name, default)
    return value if value not in (None, "") else None


@dataclass(frozen=True)
class Settings:
    """Immutable settings bundle. Field grouping mirrors the JS ``agent.js``
    constants so the two implementations stay in lockstep."""

    # ── Auth ────────────────────────────────────────────────────────────────
    anthropic_api_key: str
    serper_api_key: str | None
    tavily_api_key: str | None
    github_token: str | None

    # ── LLM ─────────────────────────────────────────────────────────────────
    claude_model: str = "claude-sonnet-4-20250514"

    # ── Pipeline content limits (mirrors agent.js MAX_*) ────────────────────
    max_content_chars: int = 24000
    page_char_limit: int = 6000
    schema_max_tokens: int = 1500
    search_max_tokens: int = 5000
    extract_max_tokens: int = 12000

    # ── Retry control (internal — NOT exposed via API contract) ─────────────
    default_max_iterations: int = 2

    # ── HTTP timeouts ───────────────────────────────────────────────────────
    http_timeout_seconds: float = 30.0
    jina_timeout_seconds: float = 15.0


_settings: Settings | None = None


def get_settings() -> Settings:
    """Build (and cache) the Settings object on first call."""
    global _settings
    if _settings is not None:
        return _settings

    anthropic_key = _env("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required. Set it in backend_py/.env "
            "(copy .env.example to .env) or in the process environment."
        )

    _settings = Settings(
        anthropic_api_key=anthropic_key,
        serper_api_key=_env("SERPER_API_KEY"),
        tavily_api_key=_env("TAVILY_API_KEY"),
        github_token=_env("GITHUB_PERSONAL_ACCESS_TOKEN") or _env("GITHUB_TOKEN"),
        claude_model=_env("CLAUDE_MODEL") or "claude-sonnet-4-20250514",
    )
    return _settings
