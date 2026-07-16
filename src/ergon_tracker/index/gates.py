"""Data-quality gates: validate a freshly-built index BEFORE it's published.

"Good-or-nothing publish": if any gate fails, the build keeps the previous snapshot live and
exits non-zero, so a broken crawl (e.g. a provider went dark and rows cratered) never ships a
degraded index to users. Each gate records actual-vs-threshold for auditability (gates.json).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import SCHEMA_VERSION, connect


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


@dataclass
class GateReport:
    results: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "gates": [
                {"name": r.name, "passed": r.passed, "detail": r.detail} for r in self.results
            ],
        }

    def summary(self) -> str:
        return "; ".join(
            f"{r.name}={'ok' if r.passed else 'FAIL'}({r.detail})" for r in self.results
        )


def evaluate_gates(
    db_path: Path | str,
    *,
    prev_row_count: int | None = None,
    last_known_rows: int | None = None,
    allow_cold_start: bool = False,
    min_ratio: float = 0.75,
) -> GateReport:
    """Run all publish gates against a built index. Pure read; never mutates the DB.

    ``prev_row_count`` is the live previous snapshot's row count (None if it's absent on disk).
    ``last_known_rows`` is a DURABLE fallback floor — the last successfully published row count
    recovered from history.jsonl — used when the live prev is missing so that a collapse can't
    masquerade as a cold start and publish over a good large snapshot (a download failure must not
    weaken the floor). Set ``allow_cold_start`` (an explicit operator decision) to permit publishing
    below the historical floor for a genuine first build or intentional reset.
    """
    rep = GateReport()
    con = connect(db_path, read_only=True)
    try:
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        rep.results.append(GateResult("integrity_check", integ == "ok", integ))

        sv = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        sv_ok = bool(sv) and int(sv[0]) == SCHEMA_VERSION
        rep.results.append(GateResult("schema_version", sv_ok, f"{sv[0] if sv else None}"))

        rows = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        # Prefer the live prev count; fall back to the durable last-published count so a MISSING
        # prev snapshot (failed download) can't silently drop the floor to >0 and let a collapse
        # overwrite a good large release.
        basis = prev_row_count or last_known_rows
        if basis and not allow_cold_start:
            floor = int(basis * min_ratio)
            src = "prev" if prev_row_count else "history[live prev MISSING]"
            rep.results.append(
                GateResult(
                    "row_floor", rows >= floor, f"{rows} rows (floor {floor}, {src} {basis})"
                )
            )
        else:
            reason = "cold start override" if (basis and allow_cold_start) else "cold start"
            rep.results.append(
                GateResult("row_floor", rows > 0, f"{rows} rows ({reason}, need >0)")
            )

        dups = con.execute("SELECT COUNT(*) - COUNT(DISTINCT id) FROM jobs").fetchone()[0]
        rep.results.append(GateResult("no_duplicate_ids", dups == 0, f"{dups} duplicates"))

        orphans = con.execute(
            "SELECT COUNT(*) FROM jobs j LEFT JOIN companies c ON j.company_key=c.company_key "
            "WHERE j.company_key IS NOT NULL AND c.company_key IS NULL"
        ).fetchone()[0]
        rep.results.append(GateResult("company_fk_intact", orphans == 0, f"{orphans} orphan rows"))
    finally:
        con.close()
    return rep
