"""Evaluate ``ergon_tracker.dedup``'s cross-source merge quality: which ``JobPosting`` pairs the
fuzzy title+company blocking considers plausible near-duplicates (``sample_pairs``), and how well
the REAL ``deduplicate()`` merge decision agrees with a fleet-judged "same role?" label set
(``score_dedup``).

``sample_pairs`` intentionally does NOT apply ``deduplicate()``'s level/location gates -- it's a
recall-oriented CANDIDATE generator (same blocking key + fuzzy title threshold ``deduplicate()``
uses, see its module docstring) meant to be handed to a labeler, so borderline pairs a stricter
merge would reject (e.g. same role, different stated level) are still surfaced for a human "same
role?" judgment. ``score_dedup`` then checks whether ``deduplicate()``'s actual (gated) merge
decision agrees with that label -- which is where a level/location gate that's too strict (or too
loose) shows up as a recall (or precision) miss.

Pure / in-memory / no network.
"""

from __future__ import annotations

from itertools import combinations

from rapidfuzz import fuzz

from ergon_tracker.dedup import deduplicate, normalize_company, normalize_title
from ergon_tracker.models import JobPosting

__all__ = ["pair_key", "sample_pairs", "predicted_merges", "score_dedup"]


def pair_key(a: JobPosting, b: JobPosting) -> frozenset[str]:
    """Canonical, order-independent identity for a candidate pair: the two postings'
    ``source:source_job_id`` provenance keys. Used both as the dict key a labeler judges
    ("same role?") and as the key ``predicted_merges``/``score_dedup`` compare against.
    """
    return frozenset((f"{a.source}:{a.source_job_id}", f"{b.source}:{b.source_job_id}"))


def sample_pairs(
    jobs: list[JobPosting], *, threshold: float = 90.0
) -> list[tuple[JobPosting, JobPosting]]:
    """Candidate near-duplicate PAIRS: every ``(a, b)`` sharing a normalized company (``dedup.
    normalize_company``) whose normalized titles (``dedup.normalize_title``) clear a
    ``token_sort_ratio`` of ``threshold`` -- the same blocking key + fuzzy gate
    ``ergon_tracker.dedup.deduplicate`` uses (mirrors its default ``threshold=90.0``), returning
    PAIRS instead of merged clusters so each candidate can be judged independently.

    Comparison only ever happens within a company block (never O(n^2) over the whole list),
    mirroring ``deduplicate()``'s own blocking scope.
    """
    by_company: dict[str, list[JobPosting]] = {}
    for job in jobs:
        by_company.setdefault(normalize_company(job.company), []).append(job)

    pairs: list[tuple[JobPosting, JobPosting]] = []
    for bucket in by_company.values():
        for a, b in combinations(bucket, 2):
            score = fuzz.token_sort_ratio(normalize_title(a.title), normalize_title(b.title))
            if score >= threshold:
                pairs.append((a, b))
    return pairs


def predicted_merges(jobs: list[JobPosting]) -> set[frozenset[str]]:
    """Which provenance-key pairs the REAL ``deduplicate()`` actually collapsed into one merged
    posting -- derived from each merged posting's own unioned ``provenance`` (not a
    reimplementation of ``deduplicate()``'s clustering), so this is ground truth for "did dedup
    merge these two records", including its level/location gates.
    """
    merged = deduplicate(jobs)
    out: set[frozenset[str]] = set()
    for job in merged:
        keys = sorted({f"{p.source}:{p.source_job_id}" for p in job.provenance})
        for a, b in combinations(keys, 2):
            out.add(frozenset((a, b)))
    return out


def score_dedup(jobs: list[JobPosting], labels: dict[frozenset[str], bool]) -> dict[str, float]:
    """Precision/recall of ``deduplicate()``'s merge decisions against a fleet-judged "same role?"
    label set.

    ``labels`` maps a pair key (``pair_key(a, b)``) to whether a human/fleet reviewer judged the
    two postings the SAME role (True) or genuinely different (False). Only labeled pairs are
    scored -- an unlabeled candidate from ``sample_pairs`` is never assumed negative.

    Returns ``{n, tp, fp, fn, precision, recall}``, where:
      * precision = of the labeled pairs ``deduplicate()`` actually merged, the fraction the
        label agrees are the same role (``tp / (tp + fp)``).
      * recall = of the labeled pairs a human says ARE the same role, the fraction
        ``deduplicate()`` actually merged (``tp / (tp + fn)``).
    Both are ``0.0`` (not undefined) when their denominator is zero.
    """
    predicted = predicted_merges(jobs)
    tp = fp = fn = 0
    for pair, is_same_role in labels.items():
        was_merged = pair in predicted
        if was_merged and is_same_role:
            tp += 1
        elif was_merged and not is_same_role:
            fp += 1
        elif not was_merged and is_same_role:
            fn += 1
        # was_merged is False and is_same_role is False -> true negative, not scored (matches
        # precision/recall convention: TN doesn't enter either ratio).

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "n": float(len(labels)),
        "tp": float(tp),
        "fp": float(fp),
        "fn": float(fn),
        "precision": precision,
        "recall": recall,
    }
