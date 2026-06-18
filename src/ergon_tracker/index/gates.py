"""Data-quality gates: validate a freshly-built index BEFORE it's published.

"Good-or-nothing publish": if any gate fails, the build keeps the previous snapshot live and
exits non-zero, so a broken crawl (e.g. a provider went dark and rows cratered) never ships a
degraded index to users. Each gate records actual-vs-threshold for auditability (gates.json).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "gates": [{"name": r.name, "passed": r.passed, "detail": r.detail} for r in self.results],
        }

    def summary(self) -> str:
        return "; ".join(f"{r.name}={'ok' if r.passed else 'FAIL'}({r.detail})" for r in self.results)


def evaluate_gates(
    db_path: Path | str,
    *,
    prev_row_count: int | None = None,
    min_ratio: float = 0.75,
) -> GateReport:
    """Run all publish gates against a built index. Pure read; never mutates the DB."""
    rep = GateReport()
    con = connect(db_path, read_only=True)
    try:
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        rep.results.append(GateResult("integrity_check", integ == "ok", integ))

        sv = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        sv_ok = bool(sv) and int(sv[0]) == SCHEMA_VERSION
        rep.results.append(GateResult("schema_version", sv_ok, f"{sv[0] if sv else None}"))

        rows = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if prev_row_count:
            floor = int(prev_row_count * min_ratio)
            rep.results.append(
                GateResult("row_floor", rows >= floor, f"{rows} rows (floor {floor}, prev {prev_row_count})")
            )
        else:
            rep.results.append(GateResult("row_floor", rows > 0, f"{rows} rows (cold start, need >0)"))

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
