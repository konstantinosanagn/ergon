"""Benchmark-driven recall/precision gate for the French (FR) extractors.

Mirrors ``test_german_recall.py``, but for the French vocab added to ``yoe.py`` / ``degree.py`` /
``comp.py`` (``language="fr"``). The corpora (``tests/fixtures/fr_{yoe,degree,salary}_corpus.jsonl``)
are blind-labeled real French postings (join/smartrecruiters, France), reconciled to the
extractors' schema/conventions:

* degree: ``level == "phd"`` is mapped to ``"phd_md"``, the schema's actual value.
* degree: ``level == "vocational"`` (CAP/BEP, not on the academic ladder) is reclassified to
  ``"associate"`` IF the record's text also contains a tertiary-short-cycle marker
  (BTS|DUT|Bac+2|DEUG — i.e. the posting ALSO offers a genuine associate-level path), else it is
  relabeled to ``expect = null`` (a correctly-unpopulated negative, mirroring the German
  Ausbildung/Lehre convention).
* yoe / salary: kept as labeled.

Record format::

    {"text": ..., "lang": "fr", "src": ..., "expect": {...} | null}

Three test functions (yoe / degree / salary), each printing recall (of the non-null expects, how
many the extractor matched) and precision (of the extractor's non-null outputs, how many were
correct — nulls the extractor wrongly populates are precision misses).

PROVISIONAL gates: set a margin below the measured numbers (2026-07-13, same corpora: yoe 180
recs/77 positives, degree 134 recs/53 positives, salary 65 recs/13 positives) — this is a first
vocab pass (vocab + benchmark + honest measurement); a follow-up pass targets the remaining gaps
(see the measured false-negative/false-positive notes returned alongside this corpus). Measured:
yoe recall 96.1% (74/77) / precision 100.0% (103/103); degree recall 92.5% (49/53) / precision
84.0% (68/81); salary recall 92.3% (12/13) / precision 98.1% (51/52). Gates below sit a full-record
margin under these (these corpora are small — one flipped record swings the percentage several
points, so a large safety margin is used rather than the ~2-5pp margin on the English corpora).
"""

from __future__ import annotations

import json
from pathlib import Path

from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.comp import CompExtractor
from ergon_tracker.extract.degree import DegreeExtractor
from ergon_tracker.extract.yoe import YoeExtractor

FIXTURES = Path(__file__).parent / "fixtures"

_YOE = YoeExtractor()
_DEGREE = DegreeExtractor()
_COMP = CompExtractor()

TOLERANCE = 0.05

YOE_RECALL_GATE = 0.90
YOE_PRECISION_GATE = 0.97
DEGREE_LEVEL_RECALL_GATE = 0.85
DEGREE_PRECISION_GATE = 0.78
SALARY_RECALL_GATE = 0.85
SALARY_PRECISION_GATE = 0.95


def _load(name: str) -> tuple[list[dict], list[dict]]:
    path = FIXTURES / name
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    positives = [r for r in records if r["expect"]]
    negatives = [r for r in records if not r["expect"]]
    return positives, negatives


# --- yoe -------------------------------------------------------------------------------------


def _yoe_extract(text: str) -> tuple[int | None, int | None]:
    return _YOE.extract(ExtractInput(title="Rôle", description_text=text, language="fr"))


def test_french_yoe_recall_and_precision() -> None:
    positives, negatives = _load("fr_yoe_corpus.jsonl")

    hits, fn = 0, []
    for r in positives:
        got = _yoe_extract(r["text"])
        want = (r["expect"]["min"], r["expect"]["max"])
        if got == want:
            hits += 1
        else:
            fn.append((r, got))
    recall = hits / len(positives) if positives else 1.0

    fp = []
    for r in negatives:
        got = _yoe_extract(r["text"])
        if got != (None, None):
            fp.append((r, got))
    precision = (len(negatives) - len(fp)) / len(negatives) if negatives else 1.0

    print(f"\n=== fr yoe: N positives={len(positives)} N negatives={len(negatives)} ===")
    print(f"fr yoe recall: {hits}/{len(positives)} = {recall:.1%}")
    print(f"fr yoe precision: {len(negatives) - len(fp)}/{len(negatives)} = {precision:.1%}")

    if fn:
        print(f"-- fr yoe FALSE NEGATIVES ({len(fn)}) --")
        for r, got in fn:
            print(f"  want={r['expect']} got={got} src={r['src']} :: {r['text']!r}")
    if fp:
        print(f"-- fr yoe FALSE POSITIVES ({len(fp)}) --")
        for r, got in fp:
            print(f"  got={got} src={r['src']} :: {r['text']!r}")

    assert recall >= YOE_RECALL_GATE, f"yoe recall {recall:.1%} below gate {YOE_RECALL_GATE:.0%}"
    assert precision >= YOE_PRECISION_GATE, (
        f"yoe precision {precision:.1%} below gate {YOE_PRECISION_GATE:.0%}"
    )


# --- degree ----------------------------------------------------------------------------------


def _degree_extract(text: str) -> tuple[str | None, bool | None]:
    return _DEGREE.extract(ExtractInput(title="Rôle", description_text=text, language="fr"))


def test_french_degree_recall_and_precision() -> None:
    positives, negatives = _load("fr_degree_corpus.jsonl")

    hits, fn = 0, []
    for r in positives:
        got_level, _got_scope = _degree_extract(r["text"])
        if got_level == r["expect"]["level"]:
            hits += 1
        else:
            fn.append((r, got_level))
    recall = hits / len(positives) if positives else 1.0

    fp = []
    for r in negatives:
        got = _degree_extract(r["text"])
        if got != (None, None):
            fp.append((r, got))
    precision = (len(negatives) - len(fp)) / len(negatives) if negatives else 1.0

    print(f"\n=== fr degree: N positives={len(positives)} N negatives={len(negatives)} ===")
    print(f"fr degree level recall: {hits}/{len(positives)} = {recall:.1%}")
    print(f"fr degree precision: {len(negatives) - len(fp)}/{len(negatives)} = {precision:.1%}")

    if fn:
        print(f"-- fr degree FALSE NEGATIVES ({len(fn)}) --")
        for r, got_level in fn:
            print(f"  want={r['expect']} got_level={got_level!r} src={r['src']} :: {r['text']!r}")
    if fp:
        print(f"-- fr degree FALSE POSITIVES ({len(fp)}) --")
        for r, got in fp:
            print(f"  got={got} src={r['src']} :: {r['text']!r}")

    assert recall >= DEGREE_LEVEL_RECALL_GATE, (
        f"degree level recall {recall:.1%} below gate {DEGREE_LEVEL_RECALL_GATE:.0%}"
    )
    assert precision >= DEGREE_PRECISION_GATE, (
        f"degree precision {precision:.1%} below gate {DEGREE_PRECISION_GATE:.0%}"
    )


# --- salary ----------------------------------------------------------------------------------


def _close(got: float | None, want: float | None) -> bool:
    if want is None:
        return got is None
    return got is not None and abs(got - want) <= TOLERANCE * want


def _salary_matches(record: dict) -> bool:
    out = _COMP.extract(ExtractInput(title="Rôle", description_text=record["text"], language="fr"))
    if out is None:
        return False
    want = record["expect"]
    return (
        _close(out.min_amount, want["min"])
        and _close(out.max_amount, want["max"])
        and out.interval is not None
        and out.interval.value == want["interval"]
    )


def test_french_salary_recall_and_precision() -> None:
    positives, negatives = _load("fr_salary_corpus.jsonl")

    hits, fn = 0, []
    for r in positives:
        if _salary_matches(r):
            hits += 1
        else:
            got = _COMP.extract(
                ExtractInput(title="Rôle", description_text=r["text"], language="fr")
            )
            fn.append((r, got))
    recall = hits / len(positives) if positives else 1.0

    fp = []
    for r in negatives:
        got = _COMP.extract(ExtractInput(title="Rôle", description_text=r["text"], language="fr"))
        if got is not None:
            fp.append((r, got))
    precision = (len(negatives) - len(fp)) / len(negatives) if negatives else 1.0

    print(f"\n=== fr salary: N positives={len(positives)} N negatives={len(negatives)} ===")
    print(f"fr salary recall: {hits}/{len(positives)} = {recall:.1%}")
    print(f"fr salary precision: {len(negatives) - len(fp)}/{len(negatives)} = {precision:.1%}")

    if fn:
        print(f"-- fr salary FALSE NEGATIVES ({len(fn)}) --")
        for r, got in fn:
            print(f"  want={r['expect']} got={got} src={r['src']} :: {r['text']!r}")
    if fp:
        print(f"-- fr salary FALSE POSITIVES ({len(fp)}) --")
        for r, got in fp:
            print(f"  got={got} src={r['src']} :: {r['text']!r}")

    assert recall >= SALARY_RECALL_GATE, (
        f"salary recall {recall:.1%} below gate {SALARY_RECALL_GATE:.0%}"
    )
    assert precision >= SALARY_PRECISION_GATE, (
        f"salary precision {precision:.1%} below gate {SALARY_PRECISION_GATE:.0%}"
    )
