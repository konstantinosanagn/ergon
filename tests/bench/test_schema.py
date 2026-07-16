from pathlib import Path

from scripts.bench.schema import FIELDS, corpus_row, read_jsonl, write_jsonl


def test_fields_cover_every_benchmarked_filter():
    assert set(FIELDS) >= {
        "level",
        "sector",
        "country",
        "city",
        "remote",
        "employment_type",
        "salary",
        "yoe",
        "degree",
        "sponsorship",
        "posted_at",
        "visa_sponsor",
    }


def test_corpus_row_defaults_and_roundtrip(tmp_path: Path):
    row = corpus_row(id="greenhouse:1", source="greenhouse", title="Engineer")
    assert row["description_text"] == "" and row["structured_salary"] is None
    p = tmp_path / "c.jsonl"
    write_jsonl(p, [row])
    assert read_jsonl(p) == [row]
