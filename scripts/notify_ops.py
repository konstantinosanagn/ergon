#!/usr/bin/env python3
"""GitHub-native operational alerting: open OR update a DEDUPLICATED issue as the alert channel.

The daily crons (``build-index.yml``, ``freshness-sweep.yml``) had NO alerting -- a broken build,
a failing data-quality gate, a metrics regression, a stale-spec re-discover queue, or a
freshness expiry-rate tripwire all surfaced only in artifacts/stderr, so an operator learned about
a dead cron days later from visible index staleness. This script is the notify channel: it uses
GitHub Issues (no Slack/email/secret dependency -- only the built-in ``GH_TOKEN`` the workflows
already have) as the durable alert store, and DEDUPLICATES so a persistently-broken cron comments
on ONE open issue per (workflow, kind) rather than spamming N new issues a day.

DEDUP MODEL: every alert has a STABLE title derived from ``--title-key`` (or, absent it, from the
workflow + kind), e.g. ``🔴 build-index: failing``. We search open issues carrying a fixed label
(``ops-alert``) for that exact title. If one is open -> add a COMMENT (run URL + details +
timestamp). Else -> CREATE it with the label. When the underlying condition is fixed an operator
closes the issue; the next incident re-opens a fresh one.

BEST-EFFORT / NON-FATAL BY CONSTRUCTION: alerting must NEVER fail a build. Every ``gh`` call goes
through :func:`_run_gh`, which swallows a missing binary, a non-zero exit, or a timeout with a
clear stderr note and returns a sentinel; :func:`main` always returns ``0``. The worst case of any
failure here is a missed alert, never a failed cron.

CLI:
  python scripts/notify_ops.py --kind failure --workflow build-index \\
      --run-url "$RUN_URL" --title-key "build-index: failing"
  python scripts/notify_ops.py --kind warning --workflow build-index --run-url "$RUN_URL" \\
      --from-json dist/gates.json --from-json dist/metrics_regression.json \\
      --from-json dist/rediscover_queue.json

Signal shapes understood by ``--from-json`` (auto-detected; multiple allowed):
  * gates.json                -> {"passed": bool, "gates": [{"name","passed","detail"}, ...]}
  * metrics_regression.json   -> {"ok": bool, "build_id": str,
                                  "regressions": [{"metric","prev","cur","delta_pct","threshold"}]}
  * rediscover_queue.json      -> ["stale-token", ...]  (a bare JSON list of stale spec tokens)
  * expiry_alarm.json          -> {"fired": ["source", ...], "total_expired": int}

For ``--kind warning`` with ``--from-json`` inputs, the alert is SUPPRESSED unless at least one
signal actually tripped (see :func:`signal_tripped`) -- so the workflow can pass the files
unconditionally and this decides whether there is anything worth alerting on.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LABEL = "ops-alert"
_GH_TIMEOUT_S = 60

# Kind -> the leading glyph the stable issue title starts with. Failures are louder than the
# WARN-only tripwires, but both dedup on the SAME title per (workflow, kind).
_EMOJI = {"failure": "🔴", "warning": "⚠️"}


# --------------------------------------------------------------------------------------------- gh
def _run_gh(args: list[str]) -> tuple[int, str]:
    """Run ``gh <args>`` best-effort. Returns ``(returncode, stdout)``; any failure (binary
    missing, non-zero exit, timeout, or any other OS error) is swallowed with a clear stderr note
    and reported as a non-zero return code -- NEVER raised. Alerting must never fail the build."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - missing binary / timeout / anything -> non-fatal
        print(f"notify_ops: gh invocation failed ({exc!r}); alert skipped (non-fatal)",
              file=sys.stderr)
        return (1, "")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        print(f"notify_ops: `gh {args[0] if args else ''}` exited {proc.returncode}: "
              f"{detail} (non-fatal)", file=sys.stderr)
    return (proc.returncode, proc.stdout or "")


def find_open_issue(title: str, label: str) -> int | None:
    """Return the number of an OPEN, ``label``-tagged issue whose title matches ``title`` exactly,
    or ``None`` if none is open (or the lookup failed -- best-effort, so a failed search degrades to
    "create a new issue", never an exception)."""
    rc, out = _run_gh([
        "issue", "list", "--state", "open", "--label", label,
        "--search", f'in:title "{title}"', "--json", "number,title", "--limit", "50",
    ])
    if rc != 0 or not out.strip():
        return None
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return None
    for it in items:
        # gh's --search is a fuzzy match; require an EXACT title equality so a near-miss title
        # never dedups onto the wrong issue.
        if isinstance(it, dict) and it.get("title") == title:
            num = it.get("number")
            if isinstance(num, int):
                return num
    return None


def _ensure_label(label: str) -> None:
    """Best-effort: create ``label`` so ``issue create --label`` can't fail on a fresh repo. A
    pre-existing label makes ``gh label create`` exit non-zero -- harmless, swallowed by _run_gh."""
    _run_gh(["label", "create", label, "--color", "B60205",
             "--description", "Automated ops alert (notify_ops.py)"])


def create_issue(title: str, body: str, label: str) -> bool:
    """Create a labelled issue. Returns True on success (best-effort; never raises)."""
    _ensure_label(label)
    rc, _ = _run_gh(["issue", "create", "--title", title, "--body", body, "--label", label])
    return rc == 0


def comment_issue(number: int, body: str) -> bool:
    """Comment on an existing issue. Returns True on success (best-effort; never raises)."""
    rc, _ = _run_gh(["issue", "comment", str(number), "--body", body])
    return rc == 0


# ---------------------------------------------------------------------------------------- payloads
def build_title(kind: str, workflow: str, title_key: str | None = None) -> str:
    """Stable, dedup-key issue title. Deterministic per (workflow, kind) when ``title_key`` is
    omitted, so recurring incidents always resolve to the SAME issue."""
    core = title_key or f"{workflow}: {'failing' if kind == 'failure' else 'tripwire'}"
    return f"{_EMOJI.get(kind, '🔔')} {core}"


def format_issue_body(kind: str, workflow: str, run_url: str, timestamp: str, detail: str) -> str:
    """The body for a NEWLY-opened alert issue (compact + actionable)."""
    label = "FAILURE" if kind == "failure" else "TRIPWIRE"
    lines = [
        f"{_EMOJI.get(kind, '🔔')} **{workflow}** — {label}",
        "",
        f"- Run: {run_url or '(no run url)'}",
        f"- First seen: {timestamp}",
    ]
    if detail.strip():
        lines += ["", detail.strip()]
    lines += [
        "",
        "---",
        "_Opened by `scripts/notify_ops.py` (GitHub-native ops alerting). Close this issue once "
        "the condition is resolved; the next incident re-opens a fresh one. Recurrences comment "
        "here rather than spawning duplicates._",
    ]
    return "\n".join(lines)


def format_comment_body(kind: str, workflow: str, run_url: str, timestamp: str, detail: str) -> str:
    """The body for a RECURRENCE comment on an already-open alert issue."""
    verb = "still failing" if kind == "failure" else "tripwire fired again"
    lines = [
        f"{_EMOJI.get(kind, '🔔')} **{workflow}** — {verb} — {timestamp}",
        "",
        f"- Run: {run_url or '(no run url)'}",
    ]
    if detail.strip():
        lines += ["", detail.strip()]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------- summarizers
def summarize_gates(data: dict[str, Any]) -> str:
    """One compact block naming the FAILING gates (name + detail)."""
    failed = [g for g in data.get("gates", []) if not g.get("passed", True)]
    if not failed:
        return "gates: all passed"
    lines = [f"- {g.get('name', '?')}: {g.get('detail', '')}" for g in failed]
    return f"**Failing gates ({len(failed)}):**\n" + "\n".join(lines)


def summarize_metrics_regression(data: dict[str, Any]) -> str:
    """One compact block naming the regressed metrics (prev -> cur, delta vs threshold)."""
    regs = data.get("regressions", []) or []
    if data.get("ok", True) and not regs:
        return "metrics: no regressions"
    lines = []
    for r in regs:
        try:
            delta = f"{float(r.get('delta_pct')):+.1f}%"
        except (TypeError, ValueError):
            delta = str(r.get("delta_pct"))
        lines.append(
            f"- {r.get('metric', '?')}: {r.get('prev')} -> {r.get('cur')} "
            f"({delta}, threshold {r.get('threshold')})"
        )
    build = data.get("build_id", "?")
    header = f"**Metrics regressions ({len(regs)}, build {build}):**"
    return header + "\n" + "\n".join(lines) if lines else header


def summarize_rediscover(data: list[Any]) -> str:
    """One compact line naming the stale spec tokens (capped) queued for re-discovery."""
    if not data:
        return "rediscover queue: empty"
    top = [str(t) for t in data[:10]]
    more = f" (+{len(data) - 10} more)" if len(data) > 10 else ""
    return f"**Stale specs ({len(data)}):** " + ", ".join(top) + more


def summarize_expiry_alarm(data: dict[str, Any]) -> str:
    """One compact line naming the sources whose freshness expiry rate spiked (soft-404 drift)."""
    fired = data.get("fired", []) or []
    if not fired:
        return "expiry alarms: none"
    total = data.get("total_expired")
    suffix = f"; {total} expired total" if total is not None else ""
    return f"**Expiry-rate alarm ({len(fired)} source(s)):** " + ", ".join(map(str, fired)) + suffix


def summarize(data: Any) -> str:
    """Dispatch a parsed signal payload to the matching summarizer by SHAPE (auto-detect)."""
    if isinstance(data, list):
        return summarize_rediscover(data)
    if isinstance(data, dict):
        if "gates" in data:
            return summarize_gates(data)
        if "regressions" in data:
            return summarize_metrics_regression(data)
        if "fired" in data:
            return summarize_expiry_alarm(data)
    return "(unrecognized signal shape)"


def signal_tripped(data: Any) -> bool:
    """True if a parsed signal payload represents a condition worth alerting on."""
    if isinstance(data, list):  # rediscover queue: non-empty = stale specs to re-discover
        return len(data) > 0
    if isinstance(data, dict):
        if "gates" in data:
            return not data.get("passed", True) or any(
                not g.get("passed", True) for g in data.get("gates", [])
            )
        if "regressions" in data:
            return not data.get("ok", True) or bool(data.get("regressions"))
        if "fired" in data:
            return bool(data.get("fired"))
    return False


def _load_json(path: str) -> Any | None:
    """Read+parse a signal file best-effort. A missing/malformed file logs a note and returns
    ``None`` (skipped) rather than raising -- a broken artifact must never break the alert."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        print(f"notify_ops: {path} not found; skipping", file=sys.stderr)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"notify_ops: could not read {path} ({exc}); skipping", file=sys.stderr)
        return None


# ------------------------------------------------------------------------------------ orchestration
def notify(
    *,
    kind: str,
    workflow: str,
    run_url: str,
    timestamp: str,
    title_key: str | None = None,
    detail: str = "",
    label: str = DEFAULT_LABEL,
) -> str:
    """The dedup DECISION: comment on the one open issue if it exists, else create it.

    Returns one of ``"commented"``, ``"created"``, or ``"error"`` (the last when the chosen gh
    write failed -- still non-fatal). Pure orchestration over the module-level gh helpers, so tests
    monkeypatch :func:`_run_gh` and assert on the create-vs-comment branch taken.
    """
    title = build_title(kind, workflow, title_key)
    number = find_open_issue(title, label)
    if number is not None:
        body = format_comment_body(kind, workflow, run_url, timestamp, detail)
        return "commented" if comment_issue(number, body) else "error"
    body = format_issue_body(kind, workflow, run_url, timestamp, detail)
    return "created" if create_issue(title, body, label) else "error"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--kind", choices=("failure", "warning"), required=True)
    parser.add_argument("--workflow", required=True, help="Workflow name, e.g. build-index")
    parser.add_argument("--run-url", default="", help="URL of the triggering Actions run")
    parser.add_argument("--title-key", default=None,
                        help="Stable dedup key for the issue title (defaults to workflow+kind)")
    parser.add_argument("--detail", default="", help="Free-text detail appended to the alert")
    parser.add_argument("--from-json", action="append", default=[], metavar="PATH",
                        help="Signal file to summarize (repeatable): gates.json / "
                             "metrics_regression.json / rediscover_queue.json / expiry_alarm.json")
    parser.add_argument("--label", default=DEFAULT_LABEL, help="Fixed dedup label")
    parser.add_argument("--timestamp", default=None,
                        help="ISO timestamp for the alert (defaults to now, UTC). Passed IN so "
                             "tests are deterministic and import never calls the clock.")
    args = parser.parse_args(argv)

    # datetime is called HERE (in main), never at import -- keeps the module import side-effect-free
    # and lets tests pass an explicit --timestamp for reproducible payloads.
    timestamp = args.timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")

    details: list[str] = []
    any_tripped = False
    for path in args.from_json:
        data = _load_json(path)
        if data is None:
            continue
        if signal_tripped(data):
            any_tripped = True
        details.append(summarize(data))
    if args.detail:
        details.append(args.detail)

    # Warning mode fed by --from-json: only alert if something actually tripped. This lets the
    # workflow pass the signal files unconditionally and defer the "is there anything to say?"
    # decision to here. A failure kind, or an explicit --detail, always alerts.
    if (
        args.kind == "warning"
        and args.from_json
        and not any_tripped
        and not args.detail
    ):
        print("notify_ops: no tripwire fired; nothing to alert", file=sys.stderr)
        return 0

    detail_text = "\n\n".join(d for d in details if d and not d.startswith("("))
    action = notify(
        kind=args.kind,
        workflow=args.workflow,
        run_url=args.run_url,
        title_key=args.title_key,
        detail=detail_text,
        timestamp=timestamp,
        label=args.label,
    )
    print(f"notify_ops: {action} ({args.workflow}/{args.kind})", file=sys.stderr)
    # ALWAYS 0: alerting is best-effort and must never change a workflow's outcome.
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
