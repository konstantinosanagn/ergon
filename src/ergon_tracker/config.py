"""Lightweight configuration / secrets access.

Keyed providers (Adzuna, USAJOBS) read their API credentials from the environment.
For local development we also support a project ``.env`` file, loaded once with a tiny
zero-dependency parser (we intentionally avoid adding python-dotenv as a runtime dep).

Resolution order for any key: a real process environment variable always wins; the
``.env`` file is only a fallback (we use ``setdefault``). The ``.env`` is discovered by
walking up from the current working directory, so it works from anywhere inside the repo.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

__all__ = ["get_env", "load_dotenv"]


def _find_dotenv(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def load_dotenv() -> None:
    """Parse the nearest ``.env`` into the environment (process env always wins).

    Cached so the file is read at most once per process. Malformed lines are skipped.
    """
    path = _find_dotenv(Path.cwd())
    if path is None:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def get_env(key: str) -> str | None:
    """Return a configured value (env var or ``.env`` fallback), or ``None`` if unset/blank."""
    load_dotenv()
    value = os.environ.get(key)
    value = value.strip() if value else None
    return value or None
