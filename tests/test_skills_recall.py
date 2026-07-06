"""Benchmark-driven gate for the skills gazetteer extractor.

Built like the salary/degree/yoe corpora: real requirement/skill-section windows fetched from live
postings (``scripts/build_skills_corpus.py``, a net anchored on skill-CONTEXT cues so windows can
contain skills the gazetteer doesn't know yet), blind-labeled for the SET of concrete technical
skills present, normalized to the gazetteer's canonical vocabulary.

``skills.py`` is a DETERMINISTIC literal matcher, which shapes how the two axes are measured:

* **recall** — of the skills a human labeler named (restricted to the gazetteer vocabulary), how many
  does the extractor find? Measured against the human gold. This catches alias/boundary/plural misses.
* **precision** — a deterministic matcher can't hallucinate: every extraction is literally in the
  text, so a raw "extracted but not labeled" is almost always human UNDER-listing, not an error. The
  only real precision errors are WORD-SENSE COLLISIONS — a canonical that is also an everyday English
  word ("the rest of the team" -> rest, "excel at" -> excel, "guard rails" -> rails). So precision is
  gated as the collision rate: extractions of a collision-prone canonical that the labeler did NOT
  name, over all extractions. (Measured 2026-07-06: 5/943 = 0.5% -> 99.5% precision.)

Record format: ``{"text": ..., "skills": ["python","aws","github"], "src": ...}`` — ``skills`` is the
gold set restricted+normalized to canonical gazetteer skills. ``[]`` = a no-technical-skill window.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ergon_tracker.extract.skills import extract_skills

CORPUS_PATH = Path(__file__).parent / "fixtures" / "skills_corpus.jsonl"

# Ratcheting gates — a margin below the measured numbers (2026-07-06, 800-window corpus:
# recall 92.7%, collision-precision 99.5%); raise as skills.py improves.
RECALL_GATE = 0.88
COLLISION_PRECISION_GATE = 0.97

# Canonicals that are also common English words: a match the labeler didn't name here is a genuine
# word-sense error (unlike an unambiguous skill like "kubernetes", which is human under-listing).
_COLLISION_PRONE = {"excel", "rest", "spring", "swift", "golang", "sap", "rails", "ruby", "scala", "seo", "git"}


def _load() -> list[dict]:
    return [json.loads(line) for line in CORPUS_PATH.read_text().splitlines() if line.strip()]


def test_corpus_is_substantial() -> None:
    records = _load()
    with_skills = [r for r in records if r["skills"]]
    assert len(records) >= 300, f"only {len(records)} records in corpus"
    assert len(with_skills) >= 150, f"only {len(with_skills)} records carry a skill"


def test_recall_vs_human_labels() -> None:
    tp = gold = 0
    misses: list[dict] = []
    for r in _load():
        got = extract_skills(r["text"])
        want = set(r["skills"])
        tp += len(got & want)
        gold += len(want)
        if want - got:
            misses.append({"miss": sorted(want - got), "text": r["text"]})
    recall = tp / gold if gold else 1.0
    print(f"\nskills recall vs labels: {tp}/{gold} = {recall:.1%}")
    if recall < RECALL_GATE:
        detail = "\n".join(f"  MISS={m['miss']} :: {m['text'][:90]!r}" for m in misses[:8])
        pytest.fail(f"recall {recall:.1%} below gate {RECALL_GATE:.0%}.\n{detail}")


def test_precision_word_sense_collisions() -> None:
    """Only word-sense collisions count as precision errors (see module docstring)."""
    ext = collisions = 0
    offenders: list[dict] = []
    for r in _load():
        got = extract_skills(r["text"])
        want = set(r["skills"])
        ext += len(got)
        bad = (got - want) & _COLLISION_PRONE
        collisions += len(bad)
        if bad:
            offenders.append({"bad": sorted(bad), "text": r["text"]})
    precision = (ext - collisions) / ext if ext else 1.0
    print(f"skills collision-precision: {ext - collisions}/{ext} = {precision:.1%} ({collisions} collisions)")
    if precision < COLLISION_PRECISION_GATE:
        detail = "\n".join(f"  COLLISION={o['bad']} :: {o['text'][:90]!r}" for o in offenders[:8])
        pytest.fail(f"collision-precision {precision:.1%} below gate {COLLISION_PRECISION_GATE:.0%}.\n{detail}")
