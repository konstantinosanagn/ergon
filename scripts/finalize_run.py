"""Finalize a labeling run: aggregate consensus gold, evaluate extractors, and persist all
artifacts into a committed runs/<id>/ directory + append to runs/RUNS.md.

Run after the labeling workflow completes:
    .venv/bin/python scripts/finalize_run.py --run-id wf_xxx --label gold-2406 [--model sonnet]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from aggregate_gold import aggregate  # noqa: E402
from eval_extraction import evaluate  # noqa: E402

GOLD = ROOT / "tests" / "data" / "gold.jsonl"
JUDGE = ROOT / "data" / "judge2"
RUNS = ROOT / "runs"


def _arg(flag: str, default: str) -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def _git_sha() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT)
            .decode()
            .strip()
        )
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> None:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d")
    label = _arg("--label", "run")
    run_id = _arg("--run-id", "unknown")
    model = _arg("--model", "sonnet")
    run_dir = RUNS / f"{stamp}-{label}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1) aggregate consensus gold
    final, agg_stats = aggregate()
    GOLD.write_text("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in final))

    # 2) evaluate extractors on the new gold
    report = evaluate(final)

    # 3) persist artifacts
    run_meta = {
        "run_id": run_id,
        "label": label,
        "finished_at": now.isoformat(),
        "git_sha": _git_sha(),
        "labeler_model": model,
        "votes_per_posting": 3,
        "corpus_size": agg_stats["postings_total"],
        "postings_labeled": agg_stats["postings_labeled"],
        "judge_agents": agg_stats["out_files"],
        "total_judgments": sum(k * v for k, v in agg_stats["vote_coverage"].items()),
    }
    (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2) + "\n")
    (run_dir / "agreement.json").write_text(json.dumps(agg_stats, indent=2) + "\n")
    (run_dir / "eval.json").write_text(json.dumps(report, indent=2) + "\n")
    (run_dir / "eval.md").write_text(_eval_md(report, run_meta, agg_stats))
    # snapshot the consensus gold used for this run (so results are reproducible even if
    # tests/data/gold.jsonl later changes)
    (run_dir / "gold.jsonl").write_text(GOLD.read_text())

    # 4) archive the raw per-judge outputs (kept out of git via runs/*/raw, see .gitignore note)
    raw_tar = run_dir / "judge_raw.tar.gz"
    out_files = sorted(JUDGE.glob("out_*.jsonl"))
    if out_files:
        with tarfile.open(raw_tar, "w:gz") as tar:
            for f in out_files:
                tar.add(f, arcname=f.name)

    # 5) append to the run log
    RUNS.mkdir(exist_ok=True)
    log = RUNS / "RUNS.md"
    header = "" if log.exists() else "# Labeling / eval runs\n\n"
    line = (
        f"- **{stamp} {label}** (`{run_id}`, {model}, 3-vote): "
        f"{agg_stats['postings_labeled']}/{agg_stats['postings_total']} postings, "
        f"level {report['level_macro_f1']:.2f} F1 · country {report['country_accuracy']:.2f} · "
        f"city {report['city_accuracy']:.2f} · comp {report['comp_f1']:.2f} F1 · "
        f"yoe {report['yoe_f1']:.2f} F1 → `runs/{run_dir.name}/`\n"
    )
    log.write_text((log.read_text() if log.exists() else header) + line)

    print(f"== run finalized: runs/{run_dir.name}/ ==")
    print(
        json.dumps(
            {"meta": run_meta, "eval": report, "agreement": agg_stats["agreement_majority"]},
            indent=2,
        )
    )


def _eval_md(report: dict, meta: dict, agg: dict) -> str:
    rows = "\n".join(
        f"| {k} | {v:.3f} |" if isinstance(v, float) else f"| {k} | {v} |"
        for k, v in report.items()
    )
    return (
        f"# Eval — {meta['label']} ({meta['run_id']})\n\n"
        f"Labeler: {meta['labeler_model']}, 3-vote consensus · "
        f"{meta['postings_labeled']}/{meta['corpus_size']} postings · "
        f"{meta['total_judgments']} judgments · git {meta['git_sha']}\n\n"
        f"## Metrics\n\n| metric | value |\n|---|---|\n{rows}\n\n"
        f"## Gold positives\n\n{json.dumps(agg['positives'])}\n\n"
        f"## Inter-annotator agreement (majority>=2)\n\n{json.dumps(agg['agreement_majority'])}\n"
    )


if __name__ == "__main__":
    main()
