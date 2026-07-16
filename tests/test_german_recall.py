"""Benchmark-driven recall/precision gate for the German (DE) extractors.

Mirrors ``test_yoe_recall.py`` / ``test_degree_recall.py`` / ``test_comp_recall.py``, but for the
German vocab added to ``yoe.py`` / ``degree.py`` / ``comp.py`` (``language="de"``). The corpora
(``tests/fixtures/de_{yoe,degree,salary}_corpus.jsonl``) are blind-labeled real German postings,
reconciled to the extractors' schema/conventions:

* degree: ``level == "vocational"`` (Ausbildung/Lehre/Meister/Techniker) is relabeled to
  ``expect = null`` — vocational training is intentionally NOT on the
  ``highschool<associate<bachelor<master<phd_md`` ladder, so ``degree.py`` returns ``None`` for it
  by design; that is scored as a (correctly unpopulated) negative, not a miss.
* degree: ``level == "phd"`` is mapped to ``"phd_md"``, the schema's actual value.
* yoe / salary: kept as labeled.

Record format::

    {"text": ..., "lang": "de", "src": ..., "expect": {...} | null}

Three test functions (yoe / degree / salary), each printing recall (of the non-null expects, how
many the extractor matched) and precision (of the extractor's non-null outputs, how many were
correct — nulls the extractor wrongly populates are precision misses).
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

# Ratcheting gates — a margin below the measured numbers. Re-measured 2026-07-13 after closing
# the vocab/logic gaps the first benchmark exposed (same corpora: yoe 125 recs/39 positives,
# degree 95 recs/19 positives, salary 98 recs/17 positives):
#   * yoe recall 53.8% -> 97.4% (38/39), precision 84.9% -> 100.0% (86/86). Fixed: "mehrjährige"
#     rebanded to an open (3, None) floor; a new "Mehrere Jahre Erfahrung" band; spelled-out
#     German numbers (zwei..zehn); DE disqualifier guards for company tenure ("seit über N
#     Jahren", "vor über N Jahren", "Mit über N Jahren Erfahrung ... wir"), candidate age ("N
#     Jahre alt"), residency ("wohnhaft ... seit N Jahren"), and company scale ("am Markt"); the
#     "erste ... Erfahrung" and "fundierte"/"langjährige" vague bands gated to professional
#     context (not a bare topic/tool object). Remaining gap: "3+ Jahre Go in Production" has no
#     "Erfahrung" cue at all (implicit tech-stack tenure) — not covered by the above patterns.
#   * degree level recall 89.5% -> 100.0% (19/19), precision 75.0% -> 96.1% (73/76). Fixed:
#     "Ausbildung oder Studium" / "Studium ... oder eine (vergleichbare) Ausbildung" (vocational
#     offered as an OR-alternative) now drops the academic-degree mention entirely, since the
#     vocational arm means no degree is actually required; "Studienabschluss" and compound
#     "X-studium" ("Jurastudium") now match; "neben/nach Deinem Studium" (candidate's own ongoing
#     studies, Werkstudent-style ads) no longer counted as a requirement. Remaining gaps: bare
#     "Promotion" (German retail/marketing jargon for a promotional campaign — a false friend for
#     the academic "Promotion" gazetteer entry) in "Team-Promotion"; a "Vertragsart:" contract-type
#     line ("Selbstständige Tätigkeit oder Duales Studium") misread as a degree mention; one
#     record where "Studium im ... Bereich" is broadened by a later sentence to non-degree-holders
#     too — none of these are in the "vocational-or-degree" family the fix targets.
#   * salary recall 17.6% -> 94.1% (16/17), precision 100.0% -> 97.5% (79/81). Fixed: a trailing
#     currency SYMBOL after the number ("16,42 €", "2000€") is now captured (previously only a
#     leading symbol or a 3-letter code); "bis"/"und" (the second half of "zwischen X und Y")
#     recognized as range separators; "je"/"im" + unit and slash-glued "€/Stunde"/"€/Std." added
#     to the interval tables; a bare, unqualified single figure ("Stundenloht 13,90 €",
#     "2000€ Bruttogehalt") now resolves as a MONTH-plausible open floor (German convention)
#     rather than an English-style exact/annual default. The 2 new false positives ("60-100 € pro
#     Skript" freelance per-deliverable rate; "bis zu 50€" monthly meal-voucher perk) are a direct,
#     accepted trade-off of trusting the trailing-symbol currency signal — both are otherwise
#     unmarked bare amounts next to a comp-shaped cue word, which is exactly the pattern the
#     recall fix needed to trust. Remaining gap: a two-line "1. Ausbildungsjahr: X / 2.
#     Ausbildungsjahr: Y" apprenticeship-pay table isn't a "bis"/"und" range, so it's still missed.
# These corpora are small, so gates are set with a full-record margin (one flipped record swings
# the percentage several points), not the ~2-5pp margin used on the much larger English corpora.
YOE_RECALL_GATE = 0.90
YOE_PRECISION_GATE = 0.95
DEGREE_LEVEL_RECALL_GATE = 0.90
DEGREE_PRECISION_GATE = 0.90
SALARY_RECALL_GATE = 0.85
SALARY_PRECISION_GATE = 0.93


def _load(name: str) -> tuple[list[dict], list[dict]]:
    path = FIXTURES / name
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    positives = [r for r in records if r["expect"]]
    negatives = [r for r in records if not r["expect"]]
    return positives, negatives


# --- yoe -------------------------------------------------------------------------------------


def _yoe_extract(text: str) -> tuple[int | None, int | None]:
    return _YOE.extract(ExtractInput(title="Rolle", description_text=text, language="de"))


def test_german_yoe_recall_and_precision() -> None:
    positives, negatives = _load("de_yoe_corpus.jsonl")

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

    print(f"\n=== de yoe: N positives={len(positives)} N negatives={len(negatives)} ===")
    print(f"de yoe recall: {hits}/{len(positives)} = {recall:.1%}")
    print(f"de yoe precision: {len(negatives) - len(fp)}/{len(negatives)} = {precision:.1%}")

    if fn:
        print(f"-- de yoe FALSE NEGATIVES ({len(fn)}) --")
        for r, got in fn:
            print(f"  want={r['expect']} got={got} src={r['src']} :: {r['text']!r}")
    if fp:
        print(f"-- de yoe FALSE POSITIVES ({len(fp)}) --")
        for r, got in fp:
            print(f"  got={got} src={r['src']} :: {r['text']!r}")

    assert recall >= YOE_RECALL_GATE, f"yoe recall {recall:.1%} below gate {YOE_RECALL_GATE:.0%}"
    assert precision >= YOE_PRECISION_GATE, (
        f"yoe precision {precision:.1%} below gate {YOE_PRECISION_GATE:.0%}"
    )


# --- degree ----------------------------------------------------------------------------------


def _degree_extract(text: str) -> tuple[str | None, bool | None]:
    return _DEGREE.extract(ExtractInput(title="Rolle", description_text=text, language="de"))


def test_german_degree_recall_and_precision() -> None:
    positives, negatives = _load("de_degree_corpus.jsonl")

    hits, fn = 0, []
    for r in positives:
        got_level, _got_scope = _degree_extract(r["text"])
        if got_level == r["expect"]["degree_min"]:
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

    print(f"\n=== de degree: N positives={len(positives)} N negatives={len(negatives)} ===")
    print(f"de degree level recall: {hits}/{len(positives)} = {recall:.1%}")
    print(f"de degree precision: {len(negatives) - len(fp)}/{len(negatives)} = {precision:.1%}")

    if fn:
        print(f"-- de degree FALSE NEGATIVES ({len(fn)}) --")
        for r, got_level in fn:
            print(f"  want={r['expect']} got_level={got_level!r} src={r['src']} :: {r['text']!r}")
    if fp:
        print(f"-- de degree FALSE POSITIVES ({len(fp)}) --")
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
    out = _COMP.extract(ExtractInput(title="Rolle", description_text=record["text"], language="de"))
    if out is None:
        return False
    want = record["expect"]
    return (
        _close(out.min_amount, want["min"])
        and _close(out.max_amount, want["max"])
        and out.interval is not None
        and out.interval.value == want["interval"]
    )


def test_german_salary_recall_and_precision() -> None:
    positives, negatives = _load("de_salary_corpus.jsonl")

    hits, fn = 0, []
    for r in positives:
        if _salary_matches(r):
            hits += 1
        else:
            got = _COMP.extract(
                ExtractInput(title="Rolle", description_text=r["text"], language="de")
            )
            fn.append((r, got))
    recall = hits / len(positives) if positives else 1.0

    fp = []
    for r in negatives:
        got = _COMP.extract(ExtractInput(title="Rolle", description_text=r["text"], language="de"))
        if got is not None:
            fp.append((r, got))
    precision = (len(negatives) - len(fp)) / len(negatives) if negatives else 1.0

    print(f"\n=== de salary: N positives={len(positives)} N negatives={len(negatives)} ===")
    print(f"de salary recall: {hits}/{len(positives)} = {recall:.1%}")
    print(f"de salary precision: {len(negatives) - len(fp)}/{len(negatives)} = {precision:.1%}")

    if fn:
        print(f"-- de salary FALSE NEGATIVES ({len(fn)}) --")
        for r, got in fn:
            print(f"  want={r['expect']} got={got} src={r['src']} :: {r['text']!r}")
    if fp:
        print(f"-- de salary FALSE POSITIVES ({len(fp)}) --")
        for r, got in fp:
            print(f"  got={got} src={r['src']} :: {r['text']!r}")

    assert recall >= SALARY_RECALL_GATE, (
        f"salary recall {recall:.1%} below gate {SALARY_RECALL_GATE:.0%}"
    )
    assert precision >= SALARY_PRECISION_GATE, (
        f"salary precision {precision:.1%} below gate {SALARY_PRECISION_GATE:.0%}"
    )
