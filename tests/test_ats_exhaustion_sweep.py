"""Unit tests for the ATS-exhaustion sweep's pure decision logic (the entity-correctness guard)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ats_exhaustion_sweep import (  # noqa: E402
    _adjudicate,
    _aggregate_from_logs,
    _is_done,
    _make_candidate,
    classify_exhaustion,
)


def _raw(company: str):
    return SimpleNamespace(company=company)


def test_adjudicate_accepts_matching_entity() -> None:
    assert _adjudicate("HCA Healthcare", [_raw("HCA Healthcare")], provider=None)  # exact
    assert _adjudicate(
        "Targa Resources", [_raw("Targa Resources Corp")], provider=None
    )  # suffix-stripped
    assert _adjudicate(
        "Saama Technologies", [_raw("noise"), _raw("Saama")], provider=None
    )  # any of sample


def test_adjudicate_rejects_namesake() -> None:
    assert not _adjudicate("Vici Properties", [_raw("Vici Collection")], provider=None)
    assert not _adjudicate("PPL Corporation", [_raw("Providence Public Library")], provider=None)


def test_adjudicate_empty_is_miss() -> None:
    assert not _adjudicate("Anyone", [], provider=None)


def test_adjudicate_trust_skips_entity_check_for_clean_federations() -> None:
    assert _adjudicate("Whatever", [_raw("Anything At All")], provider=None, trust=True)
    assert not _adjudicate("Whatever", [], provider=None, trust=True)  # still needs >=1 job


# --- Workday candidate shape: must carry tenant/wd/site so build_registry doesn't reject it ----
def test_make_candidate_splits_workday_composite() -> None:
    cand = _make_candidate("workday", "acme|wd5|Careers", "Acme, Inc.", "acme.com")
    assert cand["ats"] == "workday"
    assert (cand["tenant"], cand["wd"], cand["site"]) == ("acme", "wd5", "Careers")
    assert cand["company"] == "acme-inc"  # company_key slug
    assert cand["domain"] == "acme.com"


def test_make_candidate_plain_for_non_workday() -> None:
    cand = _make_candidate("greenhouse", "acme", "Acme Inc", None)
    assert cand == {"company": "acme-inc", "ats": "greenhouse", "token": "acme", "domain": None}
    assert "tenant" not in cand


def test_make_candidate_workday_without_composite_stays_plain() -> None:
    # Defensive: a workday token that isn't a 3-part composite is left as a plain token (the gate
    # will reject it rather than us fabricating tenant/wd/site).
    cand = _make_candidate("workday", "weirdtoken", "Acme Inc", None)
    assert "tenant" not in cand and cand["token"] == "weirdtoken"


# --- the rigor gate as a pure function --------------------------------------------------------
def test_classify_exhaustion_requires_rung1_inspection() -> None:
    # captured short-circuits before this is ever consulted, but model it explicitly:
    assert classify_exhaustion(captured=True, rung1_inspected=True) == "captured"
    # all rungs failed AND a careers page was actually inspected -> genuinely exhausted
    assert classify_exhaustion(captured=False, rung1_inspected=True) == "ats-exhausted"
    # all rungs failed but we never even inspected a careers page -> NOT browser-eligible
    assert (
        classify_exhaustion(captured=False, rung1_inspected=False) == "incomplete-needs-careers-url"
    )


# --- resume predicate: only definitively-terminal companies are skipped on re-run -------------
def test_is_done_terminal_statuses() -> None:
    for status in ("captured", "ats-exhausted", "already-in-registry"):
        assert _is_done({"status": status}) is True
    # non-terminal: a re-run may yet succeed (load shed, a domain added, a flaky host recovered)
    for status in ("timeout", "incomplete-needs-careers-url", "error", ""):
        assert _is_done({"status": status}) is False
    assert _is_done({}) is False


# --- cumulative aggregation from per-company logs (idempotent; no clobber across batches) ------
def test_aggregate_from_logs_rebuilds_cumulative_deduped(tmp_path: Path) -> None:
    (tmp_path / "acme.json").write_text(
        json.dumps(
            {
                "company": "acme",
                "name": "Acme",
                "domain": "acme.com",
                "rungs": [{"rung": "1", "result": "x"}],
                "status": "captured",
                "candidate": {"company": "acme", "ats": "greenhouse", "token": "acme"},
            }
        )
    )
    (tmp_path / "beta.json").write_text(
        json.dumps(
            {
                "company": "beta",
                "name": "Beta",
                "domain": "beta.com",
                "rungs": [{"rung": "1", "result": "inspected, nothing"}],
                "status": "ats-exhausted",
                "ats_exhausted": True,
            }
        )
    )
    (tmp_path / "gamma.json").write_text(
        json.dumps({"company": "gamma", "name": "Gamma", "status": "timeout"})
    )
    cands, queue = _aggregate_from_logs(tmp_path)
    assert [c["company"] for c in cands] == ["acme"]
    assert [q["company"] for q in queue] == ["beta"]
    assert queue[0]["exhaustion_log"] == [{"rung": "1", "result": "inspected, nothing"}]
    # gamma (timeout) is in neither — not captured, not proven-exhausted
