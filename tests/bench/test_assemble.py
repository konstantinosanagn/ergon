"""Synthetic end-to-end test for scripts.bench.assemble: proves the run-assembly glue's outputs
match what each downstream module actually consumes.

corpus rows -> predict_corpus() -> (fake fleet labels) -> build_audit_queue() -> (fake human
corrections) -> assemble_resolved() -> scoring.score_field() -- the last step is the whole point:
if assemble_resolved()'s output shape didn't match what score_field() consumes, this would raise
or silently return garbage metrics instead of a real number.

All inputs are synthetic and offline -- no network, no real corpus/label/correction files. Uses
the real ``predict.predict()`` (same as ``test_predict.py``), which runs the real enrichment
pipeline in-process against synthetic ``description_text`` -- no network involved.
"""

from __future__ import annotations

from scripts.bench.assemble import assemble_resolved, build_audit_queue, predict_corpus
from scripts.bench.resolve_gold import calibration_stats
from scripts.bench.schema import FIELDS, corpus_row
from scripts.bench.scoring import score_field

_CORPUS_ROWS = [
    corpus_row(
        id="greenhouse:1",
        source="greenhouse",
        company="Acme",
        title="Senior Software Engineer",
        description_text=(
            "5+ years of experience. Bachelor's degree required. $150,000-$180,000 USD."
        ),
        location_raw="New York, NY",
        apply_url="https://example.com/greenhouse/1",
    ),
    corpus_row(
        id="lever:2",
        source="lever",
        company="Beta Corp",
        title="Junior Data Analyst",
        description_text="Entry level role. No degree required.",
        location_raw="Remote",
        apply_url="https://example.com/lever/2",
    ),
    # Deliberately has no "source" set -- forces the id-prefix fallback in _derive_source.
    corpus_row(
        id="ashby:3",
        company="Gamma LLC",
        title="Staff Engineer",
        description_text="10+ years of experience leading teams. Master's degree preferred.",
        location_raw="San Francisco, CA",
        apply_url="https://example.com/ashby/3",
    ),
]


# ---------------------------------------------------------------------------
# predict_corpus()
# ---------------------------------------------------------------------------


def test_predict_corpus_returns_one_predrow_per_corpus_row_with_full_field_set():
    predrows = predict_corpus(_CORPUS_ROWS)
    assert len(predrows) == len(_CORPUS_ROWS)
    for row, predrow in zip(_CORPUS_ROWS, predrows, strict=True):
        assert predrow["id"] == row["id"]
        assert set(predrow) == {"id", "source", "pred"}
        assert set(predrow["pred"]) == set(FIELDS)


def test_predict_corpus_derives_source_from_row_when_present():
    predrows = predict_corpus(_CORPUS_ROWS)
    by_id = {p["id"]: p for p in predrows}
    assert by_id["greenhouse:1"]["source"] == "greenhouse"
    assert by_id["lever:2"]["source"] == "lever"


def test_predict_corpus_falls_back_to_id_prefix_when_source_missing():
    predrows = predict_corpus(_CORPUS_ROWS)
    by_id = {p["id"]: p for p in predrows}
    assert by_id["ashby:3"]["source"] == "ashby"


def test_predict_corpus_reads_back_real_extractor_values():
    predrows = predict_corpus(_CORPUS_ROWS)
    by_id = {p["id"]: p for p in predrows}
    senior_pred = by_id["greenhouse:1"]["pred"]
    assert senior_pred["level"] == "senior"
    assert senior_pred["salary"]["min"] == 150000


# ---------------------------------------------------------------------------
# fake fleet labels (Task-5 shape) matched by id to the corpus rows above
# ---------------------------------------------------------------------------


def _fake_labels(predrows):
    """Fleet labels that mostly AGREE with the extractor prediction (so most fields auto-accept
    downstream) but deliberately conflict on "level" for row 1 and leave "sector" uncovered by
    the extractor for row 2, so the audit queue exercises more than the "agree" path."""
    pred_by_id = {p["id"]: p["pred"] for p in predrows}

    gold_1 = dict(pred_by_id["greenhouse:1"])
    gold_1["level"] = "staff"  # conflict: extractor said "senior"

    gold_2 = dict(pred_by_id["lever:2"])

    gold_3 = dict(pred_by_id["ashby:3"])

    return [
        {"id": "greenhouse:1", "gold": gold_1, "split": {}},
        {"id": "lever:2", "gold": gold_2, "split": {}},
        {"id": "ashby:3", "gold": gold_3, "split": {}},
    ]


# ---------------------------------------------------------------------------
# build_audit_queue()
# ---------------------------------------------------------------------------


def test_build_audit_queue_items_carry_posting_context_from_corpus():
    predrows = predict_corpus(_CORPUS_ROWS)
    labels = _fake_labels(predrows)
    queue = build_audit_queue(_CORPUS_ROWS, predrows, labels, calib=10)

    assert queue, "expected at least the deliberate level conflict on row 1"
    for item in queue:
        assert {"title", "source", "company", "location_raw", "description_text", "url"} <= set(
            item
        )

    conflict_items = [i for i in queue if i["reason"] == "conflict"]
    assert any(i["id"] == "greenhouse:1" and i["field"] == "level" for i in conflict_items)
    row1_conflict = next(i for i in conflict_items if i["id"] == "greenhouse:1")
    assert row1_conflict["title"] == "Senior Software Engineer"
    assert row1_conflict["source"] == "greenhouse"
    assert row1_conflict["company"] == "Acme"
    assert row1_conflict["location_raw"] == "New York, NY"
    assert row1_conflict["url"] == "https://example.com/greenhouse/1"
    assert "5+ years" in row1_conflict["description_text"]


def test_build_audit_queue_skips_labels_with_no_matching_prediction():
    predrows = predict_corpus(_CORPUS_ROWS)
    labels = _fake_labels(predrows)
    labels.append({"id": "stale:999", "gold": {}, "split": {}})
    # Should not raise, and the stale label contributes nothing.
    queue = build_audit_queue(_CORPUS_ROWS, predrows, labels, calib=10)
    assert all(item["id"] != "stale:999" for item in queue)


def test_build_audit_queue_empty_labels_returns_empty_queue():
    predrows = predict_corpus(_CORPUS_ROWS)
    assert build_audit_queue(_CORPUS_ROWS, predrows, [], calib=10) == []


# ---------------------------------------------------------------------------
# assemble_resolved() -> scoring.score_field(): the shape-compatibility proof
# ---------------------------------------------------------------------------


def _fake_corrections():
    """A single human correction on the deliberate row-1 "level" conflict: the auditor sides with
    the extractor's "senior" over the fleet's "staff"."""
    return [{"id": "greenhouse:1", "field": "level", "verdict": "extractor"}]


def test_assemble_resolved_rows_carry_source_and_full_field_entries():
    predrows = predict_corpus(_CORPUS_ROWS)
    labels = _fake_labels(predrows)
    corrections = _fake_corrections()
    resolved = assemble_resolved(_CORPUS_ROWS, predrows, labels, corrections)

    assert len(resolved) == 3
    by_id = {r["id"]: r for r in resolved}

    for row in resolved:
        assert "source" in row and row["source"]
        assert set(row["fields"]) == set(FIELDS)
        for entry in row["fields"].values():
            assert {"value", "review_state", "extractor_value", "fleet_value"} <= set(entry)

    assert by_id["greenhouse:1"]["source"] == "greenhouse"
    assert by_id["ashby:3"]["source"] == "ashby"  # fallback via id prefix

    level_entry = by_id["greenhouse:1"]["fields"]["level"]
    assert level_entry["value"] == "senior"  # human correction sided with the extractor
    assert level_entry["review_state"] == "human"


def test_assemble_resolved_skips_labels_with_no_matching_prediction():
    predrows = predict_corpus(_CORPUS_ROWS)
    labels = _fake_labels(predrows)
    labels.append({"id": "stale:999", "gold": {}, "split": {}})
    resolved = assemble_resolved(_CORPUS_ROWS, predrows, labels, [])
    assert all(r["id"] != "stale:999" for r in resolved)
    assert len(resolved) == 3


def test_assemble_resolved_feeds_score_field_without_error():
    """The load-bearing assertion: resolved.jsonl rows, turned into the {"source", "gold", "pred"}
    shape the way scripts.bench.score._rows_for_field does it, must be accepted by
    scoring.score_field() and produce a real metric -- proving the resolve() -> scoring handoff
    this module is responsible for actually lines up."""
    predrows = predict_corpus(_CORPUS_ROWS)
    labels = _fake_labels(predrows)
    corrections = _fake_corrections()
    resolved = assemble_resolved(_CORPUS_ROWS, predrows, labels, corrections)

    field_rows = [
        {
            "source": row["source"],
            "gold": {"level": row["fields"]["level"]["value"]},
            "pred": {"level": row["fields"]["level"]["extractor_value"]},
        }
        for row in resolved
    ]
    metrics = score_field("level", field_rows)

    assert metrics["n"] == 3
    assert 0.0 <= metrics["accuracy"] <= 1.0
    assert 0.0 <= metrics["coverage"] <= 1.0
    # All 3 rows have a stated gold level (either agreed, corrected, or fleet fallback).
    assert metrics["coverage"] == 1.0
    # Row 1 (human-corrected to "senior", matching the extractor) and any auto-accepted rows are
    # both correct by construction -- expect a real, non-zero accuracy, not a placeholder 0.0.
    assert metrics["accuracy"] > 0.0


def test_assemble_resolved_every_field_scores_across_the_full_pipeline():
    """Broader sweep: every FIELDS entry (not just "level") must be scoreable off the resolved
    output for all three synthetic rows, end to end."""
    predrows = predict_corpus(_CORPUS_ROWS)
    labels = _fake_labels(predrows)
    resolved = assemble_resolved(_CORPUS_ROWS, predrows, labels, [])

    for field in FIELDS:
        field_rows = [
            {
                "source": row["source"],
                "gold": {field: row["fields"][field]["value"]},
                "pred": {field: row["fields"][field]["extractor_value"]},
            }
            for row in resolved
        ]
        metrics = score_field(field, field_rows)
        assert metrics["n"] == 3


def test_calibration_stats_over_assembled_resolved_and_corrections():
    predrows = predict_corpus(_CORPUS_ROWS)
    labels = _fake_labels(predrows)
    corrections = _fake_corrections()
    resolved = assemble_resolved(_CORPUS_ROWS, predrows, labels, corrections)

    stats = calibration_stats(resolved, corrections)
    assert stats["n_corrections"] == 1
    # The correction overturned the fleet's "staff" -> sided with the extractor's "senior",
    # which was a real (unreviewed) conflict, not a pre-existing auto-accepted agreement.
    assert stats["n_agreements_checked"] == 0
