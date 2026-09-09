"""Data-quality gates: validate a freshly-built index BEFORE it's published.

"Good-or-nothing publish": if any gate fails, the build keeps the previous snapshot live and
exits non-zero, so a broken crawl (e.g. a provider went dark and rows cratered) never ships a
degraded index to users. Each gate records actual-vs-threshold for auditability (gates.json).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import SCHEMA_VERSION, connect

# JD-coverage regression gate: max percentage-POINTS the build's JD-capture % may fall below the last
# published build before the publish is BLOCKED. Env-tunable (``ERGON_METRICS_JD_GATE_DROP_PCT``),
# mirroring the metrics-tripwire's ERGON_METRICS_* convention. Default 15 is deliberately wider than
# the tripwire's ~3pt WARN band: it tolerates ordinary daily churn AND the mid-recovery jumps (the
# drain refilling 40->76->85 always INCREASES coverage, so it never trips) while still catching a
# collapse (the 2026-07-27 76%->40% incident, a 36pt drop).
_DEF_JD_MAX_DROP_PCT = 15.0
# Absolute coverage that is self-evidently NOT a collapse, whatever the relative drop says. The
# 2026-07-26 incident bottomed at 40-47%, so this floor still blocks it by a wide margin. It exists
# because the relative rule compares a FRESH full crawl against a MATURED carry-forward baseline:
# a re-crawl legitimately lands lower (2026-09-09: 76.0% vs a 93.39% baseline, with 316,859 rows
# not yet in the detail sidecar) and would otherwise be blocked for drain lag, not data loss.
_DEF_JD_HEALTHY_PCT = 70.0


def jd_gate_drop_pct_from_env() -> float:
    """Resolve the JD-coverage gate's max-drop threshold from ``ERGON_METRICS_JD_GATE_DROP_PCT``.

    Mirrors :meth:`ergon.index.metrics_gate.MetricsThresholds.from_env`; a bad/absent value
    falls back to the default so the gate can never be disabled by a malformed override.
    """
    try:
        return float(os.environ.get("ERGON_METRICS_JD_GATE_DROP_PCT", str(_DEF_JD_MAX_DROP_PCT)))
    except (TypeError, ValueError):
        return _DEF_JD_MAX_DROP_PCT


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


def _jd_gate_result(
    con: Any,
    *,
    prev_jd_pct: float | None,
    jd_max_drop_pct: float,
    jd_healthy_pct: float = _DEF_JD_HEALTHY_PCT,
) -> GateResult:
    """JD-coverage regression check against an OPEN connection.

    ``with_jd``/``active`` mirror coverage.compute_coverage exactly, so this jd_pct is the same
    number the metrics tripwire baselines on. RELATIVE and one-directional: fails only on a DROP,
    so a recovery build always passes and no baseline passes.

    MUST be evaluated on the FINAL artifact, after the Tier-3 detail merge has filled `snippet`.
    Run pre-merge it measures crawl-time JD only, which on a full re-crawl is structurally low and
    cannot clear any baseline set by a carry-forward build. See evaluate_jd_coverage.
    """
    active = con.execute("SELECT COUNT(*) FROM jobs WHERE status='active'").fetchone()[0]
    with_jd = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE snippet IS NOT NULL AND TRIM(snippet) != '' "
        "AND status='active'"
    ).fetchone()[0]
    jd_pct = round(with_jd / active * 100, 2) if active else 0.0
    if prev_jd_pct is None:
        return GateResult("jd_coverage", True, f"{jd_pct}% (no baseline)")
    drop = prev_jd_pct - jd_pct
    if drop <= jd_max_drop_pct:
        return GateResult(
            "jd_coverage",
            True,
            f"{jd_pct}% (baseline {prev_jd_pct}%, drop {drop:+.2f}pt, max {jd_max_drop_pct})",
        )
    # Over the relative limit, but high enough in absolute terms that this cannot be a collapse.
    if jd_pct >= jd_healthy_pct:
        return GateResult(
            "jd_coverage",
            True,
            f"{jd_pct}% (baseline {prev_jd_pct}%, drop {drop:+.2f}pt over max "
            f"{jd_max_drop_pct}, but >= healthy floor {jd_healthy_pct})",
        )
    return GateResult(
        "jd_coverage",
        False,
        f"{jd_pct}% (baseline {prev_jd_pct}%, drop {drop:+.2f}pt, max {jd_max_drop_pct}, "
        f"below healthy floor {jd_healthy_pct})",
    )


def evaluate_jd_coverage(
    db_path: Path | str,
    *,
    prev_jd_pct: float | None = None,
    jd_max_drop_pct: float = _DEF_JD_MAX_DROP_PCT,
    jd_healthy_pct: float = _DEF_JD_HEALTHY_PCT,
) -> GateResult:
    """Standalone JD-coverage gate, for the POST-reconcile check on the artifact being shipped."""
    con = connect(db_path, read_only=True)
    try:
        return _jd_gate_result(
            con,
            prev_jd_pct=prev_jd_pct,
            jd_max_drop_pct=jd_max_drop_pct,
            jd_healthy_pct=jd_healthy_pct,
        )
    finally:
        con.close()


def evaluate_gates(
    db_path: Path | str,
    *,
    prev_row_count: int | None = None,
    last_known_rows: int | None = None,
    allow_cold_start: bool = False,
    min_ratio: float = 0.75,
    prev_jd_pct: float | None = None,
    jd_max_drop_pct: float = _DEF_JD_MAX_DROP_PCT,
    include_jd: bool = True,
) -> GateReport:
    """Run all publish gates against a built index. Pure read; never mutates the DB.

    ``prev_row_count`` is the live previous snapshot's row count (None if it's absent on disk).
    ``last_known_rows`` is a DURABLE fallback floor — the last successfully published row count
    recovered from history.jsonl — used when the live prev is missing so that a collapse can't
    masquerade as a cold start and publish over a good large snapshot (a download failure must not
    weaken the floor). Set ``allow_cold_start`` (an explicit operator decision) to permit publishing
    below the historical floor for a genuine first build or intentional reset.

    ``prev_jd_pct`` is the last published build's JD-capture % (``metrics.jd_pct`` from the last
    ``published`` history.jsonl row — the SAME baseline the metrics tripwire uses). The JD-coverage
    gate FAILS the publish when this build's JD % has dropped more than ``jd_max_drop_pct`` POINTS
    below that baseline, so a collapse (76%->40%) is blocked. It is RELATIVE and one-directional: a
    build that INCREASES coverage (the drain-recovery climb 40->76->85) always PASSES — only a DROP
    beyond the threshold fails. ``prev_jd_pct is None`` (first build / missing history) PASSES, never
    false-failing off an absent baseline (mirrors the tripwire's missing-prev clause).
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

        if include_jd:
            rep.results.append(
                _jd_gate_result(con, prev_jd_pct=prev_jd_pct, jd_max_drop_pct=jd_max_drop_pct)
            )
    finally:
        con.close()
    return rep
