"""Product-metric regression TRIPWIRE: compare a build's headline metrics against the previous
build and WARN when one craters.

The publish gates (:mod:`ergon_tracker.index.gates`) protect the raw row count, but the build's
*product* metrics -- JD-text capture %, sector coverage %, per-source counts, active-job count --
have no build-to-build comparison. A source going dark, or JD-capture collapsing, could ship
silently because those numbers are computed once (coverage.json) and never diffed.

This module is **observability-only and NON-FATAL by design**, mirroring
:func:`ergon_tracker.index.freshness.check_expiry_alarms`: it never blocks a publish (that is the
row-floor gate's job) and it never mutates anything. It reads the current build's metrics + the
previous build's metrics (recovered from history.jsonl), flags regressions, and emits a machine
signal (``dist/metrics_regression.json``) for the alerting layer to consume, plus a WARNING per
regression.

Baseline model: the compact ``metrics`` block is written into each build's history.jsonl record, so
the previous build is the baseline. With no prior baseline (first build, or a malformed prior
record) the report is clean (``ok=true``, no regressions) -- a tripwire must never false-alarm off a
missing baseline.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

# Regression thresholds. Defaults chosen to catch a real source-outage / capture-collapse while
# tolerating ordinary day-to-day churn; each overridable via env (ERGON_METRICS_*), following the
# repo's freshness-tripwire convention (see freshness.ERGON_FRESHNESS_EXPIRY_ALARM).
_DEF_ACTIVE_DROP_PCT = 5.0  # active_jobs falling > this % vs prev build
_DEF_SOURCE_DROP_PCT = 30.0  # any tracked source's count falling > this % vs prev build
_DEF_PCT_POINT_DROP = 3.0  # jd_pct / salary_pct / sector_pct falling > this many POINTS vs prev

_TOP_SOURCES = 15  # how many top sources the metrics baseline tracks (task spec: ~15)


@dataclass
class MetricsThresholds:
    """Trip thresholds for :func:`check_metrics_regression`. All are *drop* magnitudes (positive)."""

    active_drop_pct: float = _DEF_ACTIVE_DROP_PCT
    source_drop_pct: float = _DEF_SOURCE_DROP_PCT
    pct_point_drop: float = _DEF_PCT_POINT_DROP

    @classmethod
    def from_env(cls) -> MetricsThresholds:
        """Build thresholds from env overrides (``ERGON_METRICS_*``), falling back to defaults."""

        def _f(key: str, default: float) -> float:
            try:
                return float(os.environ.get(key, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            active_drop_pct=_f("ERGON_METRICS_ACTIVE_DROP_PCT", _DEF_ACTIVE_DROP_PCT),
            source_drop_pct=_f("ERGON_METRICS_SOURCE_DROP_PCT", _DEF_SOURCE_DROP_PCT),
            pct_point_drop=_f("ERGON_METRICS_PCT_POINT_DROP", _DEF_PCT_POINT_DROP),
        )


@dataclass
class MetricRegression:
    """One tripped metric. ``delta_pct`` and ``threshold`` are SIGNED (negative == a drop): the
    metric regressed when ``delta_pct < threshold``. For count metrics (active_jobs, per-source) the
    unit is % change; for the ``*_pct`` metrics it is percentage POINTS."""

    metric: str
    prev: float
    cur: float
    delta_pct: float
    threshold: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "prev": self.prev,
            "cur": self.cur,
            "delta_pct": self.delta_pct,
            "threshold": self.threshold,
        }


@dataclass
class MetricsRegressionReport:
    """Mirror of :class:`ergon_tracker.index.gates.GateReport` for the metrics tripwire."""

    regressions: list[MetricRegression] = field(default_factory=list)
    build_id: str = ""

    @property
    def ok(self) -> bool:
        return not self.regressions

    def to_signal(self) -> dict[str, Any]:
        """The exact ``metrics_regression.json`` schema the alerting agent consumes."""
        return {
            "ok": self.ok,
            "build_id": self.build_id,
            "regressions": [r.to_dict() for r in self.regressions],
        }

    def summary(self) -> str:
        if not self.regressions:
            return "no metric regressions"
        return "; ".join(
            f"{r.metric} {r.prev}->{r.cur} ({r.delta_pct:+.1f} vs {r.threshold:+.1f})"
            for r in self.regressions
        )


def _num(value: Any) -> float | None:
    """Coerce to float, tolerating bad types (a malformed prev record must never raise)."""
    if isinstance(value, bool):  # bool is an int subclass; never treat True/False as a metric
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def metrics_from_coverage(cov: dict[str, Any], *, top_sources: int = _TOP_SOURCES) -> dict[str, Any]:
    """Reduce a full coverage dict to the compact ``metrics`` baseline block stored in history.jsonl.

    Cheap and side-effect-free: it only reads an already-computed coverage dict. ``*_pct`` fields are
    active-relative percentages; ``sector_pct`` is the share of active rows that carry any sector.
    """
    active = int(cov.get("active_jobs", 0) or 0)
    with_jd = int(cov.get("with_jd", 0) or 0)
    with_salary = int(cov.get("with_salary", 0) or 0)
    by_sector = cov.get("by_sector") or {}
    sector_total = sum(int(v or 0) for v in by_sector.values())
    by_source = cov.get("by_source") or {}
    top = dict(
        sorted(by_source.items(), key=lambda kv: (-int(kv[1] or 0), str(kv[0])))[:top_sources]
    )

    def _pct(n: int) -> float:
        return round(n / active * 100, 2) if active else 0.0

    return {
        "active_jobs": active,
        "with_jd": with_jd,
        "jd_pct": _pct(with_jd),
        "with_salary": with_salary,
        "salary_pct": _pct(with_salary),
        "sector_pct": _pct(sector_total),
        "top_sources": top,
    }


def check_metrics_regression(
    cur: dict[str, Any],
    prev: dict[str, Any] | None,
    *,
    thresholds: MetricsThresholds | None = None,
    build_id: str = "",
) -> MetricsRegressionReport:
    """Compare the current build's ``metrics`` block against the previous build's and report any
    regression. NON-FATAL and total: a missing / empty / malformed ``prev`` yields a clean report
    (``ok=true``, no regressions) rather than an error -- a tripwire must never false-alarm off a
    bad baseline.

    Regression rules (a metric trips when its signed change is below the negative threshold):
      * ``active_jobs`` drops > ``active_drop_pct`` % ;
      * any source in the previous top-N drops > ``source_drop_pct`` % (a source that vanishes from
        the current top-N reads as 0 -> a full drop; this errs toward WARNING, which is the intent);
      * ``jd_pct`` / ``salary_pct`` / ``sector_pct`` drop > ``pct_point_drop`` percentage points.
    """
    th = thresholds or MetricsThresholds.from_env()
    bid = build_id or str(cur.get("build_id", "") or "")
    if not isinstance(prev, dict) or not prev:
        return MetricsRegressionReport(regressions=[], build_id=bid)

    regressions: list[MetricRegression] = []
    try:
        # active_jobs: % drop vs prev.
        p_active = _num(prev.get("active_jobs"))
        c_active = _num(cur.get("active_jobs"))
        if p_active and p_active > 0 and c_active is not None:
            delta = (c_active - p_active) / p_active * 100.0
            if delta < -th.active_drop_pct:
                regressions.append(
                    MetricRegression(
                        "active_jobs", p_active, c_active, round(delta, 2), -th.active_drop_pct
                    )
                )

        # percentage-point metrics.
        for name in ("jd_pct", "salary_pct", "sector_pct"):
            p_val = _num(prev.get(name))
            c_val = _num(cur.get(name))
            if p_val is None or c_val is None:
                continue
            delta = c_val - p_val
            if delta < -th.pct_point_drop:
                regressions.append(
                    MetricRegression(name, p_val, c_val, round(delta, 2), -th.pct_point_drop)
                )

        # per-source: % drop for each source present in the previous baseline's top-N.
        p_src = prev.get("top_sources")
        c_src = cur.get("top_sources")
        if isinstance(p_src, dict):
            c_src = c_src if isinstance(c_src, dict) else {}
            for src in sorted(p_src, key=str):
                pv = _num(p_src.get(src))
                if not pv or pv <= 0:
                    continue
                cv = _num(c_src.get(src)) or 0.0
                delta = (cv - pv) / pv * 100.0
                if delta < -th.source_drop_pct:
                    regressions.append(
                        MetricRegression(
                            f"source:{src}", pv, cv, round(delta, 2), -th.source_drop_pct
                        )
                    )
    except Exception:  # noqa: BLE001 - a tripwire must never raise; degrade to "nothing observed"
        return MetricsRegressionReport(regressions=[], build_id=bid)

    return MetricsRegressionReport(regressions=regressions, build_id=bid)


def log_regressions(
    report: MetricsRegressionReport, *, logger: logging.Logger | None = None
) -> None:
    """WARN once per regression, mirroring ``check_expiry_alarms``'s logging shape."""
    log = logger if logger is not None else _log
    for r in report.regressions:
        log.warning(
            "[metrics] REGRESSION: metric=%s prev=%s cur=%s delta=%+.2f threshold=%+.2f "
            "(build=%s) -- a product metric dropped build-to-build; review before it compounds",
            r.metric,
            r.prev,
            r.cur,
            r.delta_pct,
            r.threshold,
            report.build_id,
        )
