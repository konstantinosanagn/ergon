# Labeling / eval runs

Durable log of gold-labeling + extraction-eval runs. Each `runs/<date>-<label>/` holds:
`run.json` (metadata), `agreement.json` (inter-annotator agreement), `eval.json` + `eval.md`
(metrics), `gold.jsonl` (the exact consensus set scored), and `judge_raw.tar.gz` (raw per-judge
outputs). Reproduce metrics: `.venv/bin/python scripts/eval_extraction.py runs/<dir>/gold.jsonl`.

- **2026-06-16 gold-162** (opus, 3-vote): 162 postings — level 0.94 F1 · country 0.92 · city 0.96 · comp 0.96 F1 · yoe 0.93 F1 (committed in docs/extraction-baseline.md)
- **2026-06-16 gold-2406** (`wf_d2892da5-5bb`, sonnet, 3-vote): RUNNING — 234 agents, 2406 postings, 7218 judgments
