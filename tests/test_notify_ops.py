"""Tests for scripts/notify_ops.py -- the GitHub-native ops-alerting channel.

Covers the dedup DECISION (existing open issue -> comment; none -> create), payload formatting,
the ``--from-json`` signal summarizers (gates.json + metrics_regression.json + rediscover +
expiry) and their trip detection, and that any ``gh`` failure is swallowed non-fatally. All
``gh`` invocation is monkeypatched -- NOTHING here touches the network or a real ``gh`` binary.
"""

from __future__ import annotations

import json

import pytest

no = pytest.importorskip("scripts.notify_ops", reason="run from repo root")


# --------------------------------------------------------------------------------------------- gh
class FakeGh:
    """Records every ``gh`` argv and returns a canned ``(returncode, stdout)`` per subcommand.

    ``issue_list_out`` is what ``gh issue list ... --json`` returns; a returncode override lets a
    test force a failure. Substituted for ``notify_ops._run_gh`` -- the create/comment/list helpers
    all look ``_run_gh`` up on the module at call time, so this intercepts every gh call.
    """

    def __init__(self, issue_list_out: str = "[]", rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._issue_list_out = issue_list_out
        self._rc = rc

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        if self._rc != 0:
            return (self._rc, "")
        if args[:2] == ["issue", "list"]:
            return (0, self._issue_list_out)
        return (0, "")

    def ran(self, *prefix: str) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


def _kw(**over):
    base = {
        "kind": "warning",
        "workflow": "build-index",
        "run_url": "https://x/run/1",
        "timestamp": "2026-07-20T00:00:00+00:00",
        "detail": "",
    }
    base.update(over)
    return base


# ------------------------------------------------------------------------------ dedup DECISION
def test_no_open_issue_creates(monkeypatch):
    fake = FakeGh(issue_list_out="[]")
    monkeypatch.setattr(no, "_run_gh", fake)
    action = no.notify(**_kw())
    assert action == "created"
    assert fake.ran("issue", "create")
    assert not fake.ran("issue", "comment")


def test_existing_open_issue_comments(monkeypatch):
    title = no.build_title("warning", "build-index", "build-index: tripwire")
    fake = FakeGh(issue_list_out=json.dumps([{"number": 42, "title": title}]))
    monkeypatch.setattr(no, "_run_gh", fake)
    action = no.notify(**_kw(title_key="build-index: tripwire"))
    assert action == "commented"
    assert fake.ran("issue", "comment", "42")
    assert not fake.ran("issue", "create")


def test_near_miss_title_does_not_dedup(monkeypatch):
    # A different title in the search results must NOT be treated as the same issue.
    fake = FakeGh(issue_list_out=json.dumps([{"number": 9, "title": "🔴 something else"}]))
    monkeypatch.setattr(no, "_run_gh", fake)
    assert no.notify(**_kw()) == "created"


def test_failure_and_warning_titles_are_distinct():
    assert no.build_title("failure", "build-index", "build-index: failing") == "🔴 build-index: failing"
    assert no.build_title("warning", "freshness-sweep") == "⚠️ freshness-sweep: tripwire"
    assert no.build_title("failure", "freshness-sweep") == "🔴 freshness-sweep: failing"


# ---------------------------------------------------------------------------------- payloads
def test_issue_body_is_actionable():
    body = no.format_issue_body(
        "failure", "build-index", "https://x/run/7", "2026-07-20T00:00:00+00:00", "some detail"
    )
    assert "build-index" in body
    assert "https://x/run/7" in body
    assert "2026-07-20T00:00:00+00:00" in body
    assert "some detail" in body


def test_comment_body_carries_run_and_timestamp():
    body = no.format_comment_body(
        "warning", "freshness-sweep", "https://x/run/8", "2026-07-20T00:00:00+00:00", "adp spiked"
    )
    assert "https://x/run/8" in body
    assert "2026-07-20T00:00:00+00:00" in body
    assert "adp spiked" in body


# -------------------------------------------------------------------------------- summarizers
def test_summarize_gates_lists_only_failing():
    data = {
        "passed": False,
        "gates": [
            {"name": "row_floor", "passed": True, "detail": "ok"},
            {"name": "integrity_check", "passed": False, "detail": "malformed page"},
        ],
    }
    out = no.summarize(data)
    assert "integrity_check" in out
    assert "malformed page" in out
    assert "row_floor" not in out  # passing gates are omitted


def test_summarize_gates_all_passed():
    data = {"passed": True, "gates": [{"name": "x", "passed": True, "detail": "ok"}]}
    assert no.summarize(data) == "gates: all passed"


def test_summarize_metrics_regression():
    data = {
        "ok": False,
        "build_id": "build-99",
        "regressions": [
            {"metric": "total_jobs", "prev": 1000, "cur": 800, "delta_pct": -20.0, "threshold": -10.0}
        ],
    }
    out = no.summarize(data)
    assert "total_jobs" in out
    assert "build-99" in out
    assert "-20.0%" in out


def test_summarize_metrics_no_regression():
    assert no.summarize({"ok": True, "build_id": "b", "regressions": []}) == "metrics: no regressions"


def test_summarize_metrics_tolerates_nonfloat_delta():
    data = {"ok": False, "build_id": "b", "regressions": [{"metric": "m", "delta_pct": None}]}
    assert "m" in no.summarize(data)  # must not raise on a None delta


def test_summarize_rediscover_list():
    out = no.summarize(["tok-a", "tok-b"])
    assert "tok-a" in out and "tok-b" in out and "2" in out


def test_summarize_expiry_alarm():
    out = no.summarize({"fired": ["adp", "taleo"], "total_expired": 123})
    assert "adp" in out and "taleo" in out


# ----------------------------------------------------------------------------- trip detection
@pytest.mark.parametrize(
    "data,expected",
    [
        ({"passed": True, "gates": [{"name": "x", "passed": True}]}, False),
        ({"passed": False, "gates": [{"name": "x", "passed": False}]}, True),
        ({"ok": True, "regressions": []}, False),
        ({"ok": False, "regressions": [{"metric": "m"}]}, True),
        ([], False),
        (["tok"], True),
        ({"fired": [], "total_expired": 0}, False),
        ({"fired": ["adp"], "total_expired": 9}, True),
    ],
)
def test_signal_tripped(data, expected):
    assert no.signal_tripped(data) is expected


# ------------------------------------------------------------------- gh failure is swallowed
def test_gh_failure_is_non_fatal(monkeypatch):
    fake = FakeGh(rc=1)  # every gh call "fails"
    monkeypatch.setattr(no, "_run_gh", fake)
    # find returns None (list failed) -> create attempted -> rc 1 -> "error", but NO exception.
    assert no.notify(**_kw()) == "error"


def test_run_gh_swallows_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("gh not installed")

    monkeypatch.setattr(no.subprocess, "run", boom)
    rc, out = no._run_gh(["issue", "list"])
    assert rc == 1 and out == ""  # swallowed, not raised


def test_run_gh_nonzero_returncode(monkeypatch):
    class P:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(no.subprocess, "run", lambda *a, **k: P())
    rc, out = no._run_gh(["issue", "create"])
    assert rc == 1


# ------------------------------------------------------------------------------------- main()
def test_main_warning_suppressed_when_nothing_tripped(monkeypatch, tmp_path):
    fake = FakeGh()
    monkeypatch.setattr(no, "_run_gh", fake)
    gates = tmp_path / "gates.json"
    gates.write_text(json.dumps({"passed": True, "gates": [{"name": "x", "passed": True}]}))
    rc = no.main([
        "--kind", "warning", "--workflow", "build-index", "--run-url", "u",
        "--from-json", str(gates), "--timestamp", "2026-07-20T00:00:00+00:00",
    ])
    assert rc == 0
    assert not fake.ran("issue", "create")  # nothing tripped -> no alert


def test_main_warning_fires_on_failing_gate(monkeypatch, tmp_path):
    fake = FakeGh()
    monkeypatch.setattr(no, "_run_gh", fake)
    gates = tmp_path / "gates.json"
    gates.write_text(json.dumps({"passed": False, "gates": [{"name": "x", "passed": False, "detail": "d"}]}))
    rc = no.main([
        "--kind", "warning", "--workflow", "build-index", "--run-url", "u",
        "--from-json", str(gates), "--timestamp", "2026-07-20T00:00:00+00:00",
    ])
    assert rc == 0
    assert fake.ran("issue", "create")


def test_main_failure_always_alerts_and_returns_zero(monkeypatch):
    fake = FakeGh(rc=1)  # even with gh fully broken, main must return 0
    monkeypatch.setattr(no, "_run_gh", fake)
    rc = no.main([
        "--kind", "failure", "--workflow", "build-index", "--run-url", "u",
        "--detail", "boom", "--timestamp", "2026-07-20T00:00:00+00:00",
    ])
    assert rc == 0


def test_main_missing_json_is_skipped(monkeypatch, tmp_path):
    fake = FakeGh()
    monkeypatch.setattr(no, "_run_gh", fake)
    rc = no.main([
        "--kind", "warning", "--workflow", "build-index", "--run-url", "u",
        "--from-json", str(tmp_path / "nope.json"),
        "--timestamp", "2026-07-20T00:00:00+00:00",
    ])
    assert rc == 0
    assert not fake.ran("issue", "create")  # unreadable file -> nothing tripped
