"""Data shapes + JSONL IO for the filter benchmark (bench v2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Every field the benchmark scores. Grouped by regime in the report, listed flat here.
FIELDS: list[str] = [
    "level",
    "sector",
    "country",
    "city",
    "remote",
    "employment_type",
    "salary",
    "yoe",
    "degree",
    "sponsorship",
    "posted_at",
    "visa_sponsor",
]

_CORPUS_DEFAULTS: dict[str, Any] = {
    "id": "",
    "source": "",
    "company": "",
    "title": "",
    "description_text": "",
    "location_raw": "",
    "structured_salary": None,
    "apply_url": "",
    "language": "en",
    "sector_hint": None,
    "country_hint": None,
}


def corpus_row(**kw: Any) -> dict[str, Any]:
    """A corpus row with defaults filled; unknown keys are kept (forward-compatible)."""
    return {**_CORPUS_DEFAULTS, **kw}


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    Path(path).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
