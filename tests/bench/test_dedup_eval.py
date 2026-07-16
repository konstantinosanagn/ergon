"""Tests for scripts.bench.dedup_eval: candidate near-duplicate pairing and precision/recall of
``ergon_tracker.dedup.deduplicate()`` merges against a labeled "same role?" set.

All inputs are synthetic, in-memory ``JobPosting`` clusters -- no network.
"""

from __future__ import annotations

from scripts.bench.dedup_eval import pair_key, predicted_merges, sample_pairs, score_dedup

from ergon_tracker.models import JobLevel, JobPosting, Location


def _job(
    source: str, source_job_id: str, company: str, title: str, **overrides: object
) -> JobPosting:
    defaults: dict[str, object] = {
        "source": source,
        "source_job_id": source_job_id,
        "company": company,
        "title": title,
        "locations": [Location(city="New York", country="United States")],
        "level": JobLevel.SENIOR,
    }
    defaults.update(overrides)
    return JobPosting.create(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# sample_pairs() -- fuzzy title+company candidate blocking
# ---------------------------------------------------------------------------


def test_sample_pairs_flags_obvious_same_role_across_sources():
    a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer")
    b = _job("remoteok", "999", "Acme Corp", "Sr. Backend Engineer")
    pairs = sample_pairs([a, b])
    assert len(pairs) == 1
    got = {p.source_job_id for p in pairs[0]}
    assert got == {"1", "999"}


def test_sample_pairs_excludes_clearly_different_roles():
    a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer")
    b = _job("greenhouse", "2", "Acme Corp", "Product Marketing Manager")
    pairs = sample_pairs([a, b])
    assert pairs == []


def test_sample_pairs_never_compares_across_different_companies():
    a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer")
    b = _job("greenhouse", "2", "Globex Inc", "Senior Backend Engineer")
    pairs = sample_pairs([a, b])
    assert pairs == []


# ---------------------------------------------------------------------------
# predicted_merges() -- ground truth derived from the REAL deduplicate()
# ---------------------------------------------------------------------------


def test_predicted_merges_includes_a_real_cross_source_merge():
    a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer")
    b = _job("remoteok", "999", "Acme Corp", "Sr. Backend Engineer")
    merges = predicted_merges([a, b])
    assert pair_key(a, b) in merges


def test_predicted_merges_excludes_different_levels():
    a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer", level=JobLevel.SENIOR)
    b = _job("remoteok", "999", "Acme Corp", "Backend Engineer", level=JobLevel.MID)
    merges = predicted_merges([a, b])
    assert pair_key(a, b) not in merges


# ---------------------------------------------------------------------------
# score_dedup() -- precision/recall vs a labeled "same role?" set
# ---------------------------------------------------------------------------


def test_score_dedup_perfect_on_one_true_positive():
    a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer")
    b = _job("remoteok", "999", "Acme Corp", "Sr. Backend Engineer")
    labels = {pair_key(a, b): True}
    result = score_dedup([a, b], labels)
    assert result["tp"] == 1
    assert result["fp"] == 0
    assert result["fn"] == 0
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0


def test_score_dedup_false_negative_when_merge_missed():
    # Human says same role, but the level mismatch keeps deduplicate() from merging them.
    a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer", level=JobLevel.SENIOR)
    b = _job("remoteok", "999", "Acme Corp", "Backend Engineer", level=JobLevel.MID)
    labels = {pair_key(a, b): True}
    result = score_dedup([a, b], labels)
    assert result["tp"] == 0
    assert result["fn"] == 1
    assert result["recall"] == 0.0


def test_score_dedup_false_positive_when_human_disagrees_with_a_merge():
    a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer")
    b = _job("remoteok", "999", "Acme Corp", "Sr. Backend Engineer")
    labels = {pair_key(a, b): False}  # fleet judged: actually different roles
    result = score_dedup([a, b], labels)
    assert result["tp"] == 0
    assert result["fp"] == 1
    assert result["precision"] == 0.0


def test_score_dedup_mixed_cluster_precision_and_recall():
    same_a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer")
    same_b = _job("remoteok", "999", "Acme Corp", "Sr. Backend Engineer")
    missed_a = _job(
        "greenhouse", "3", "Acme Corp", "Senior Backend Engineer", level=JobLevel.SENIOR
    )
    missed_b = _job("lever", "4", "Acme Corp", "Backend Engineer", level=JobLevel.MID)

    jobs = [same_a, same_b, missed_a, missed_b]
    labels = {
        pair_key(same_a, same_b): True,  # correctly merged
        pair_key(missed_a, missed_b): True,  # human says same role, dedup missed it (fn)
    }
    result = score_dedup(jobs, labels)
    assert result["n"] == 2
    assert result["tp"] == 1
    assert result["fn"] == 1
    assert result["fp"] == 0
    assert result["precision"] == 1.0
    assert result["recall"] == 0.5


def test_score_dedup_empty_labels_returns_zero_not_error():
    a = _job("greenhouse", "1", "Acme Corp", "Senior Backend Engineer")
    result = score_dedup([a], {})
    assert result["n"] == 0
    assert result["precision"] == 0.0
    assert result["recall"] == 0.0
