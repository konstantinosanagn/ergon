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


def test_roundtrip_survives_exotic_unicode_line_separators(tmp_path: Path):
    # Real JD text carries U+2028/U+2029/NEL/VT/FF, which json.dumps(ensure_ascii=False) leaves
    # unescaped; str.splitlines() would split one record into invalid fragments. Each of these must
    # round-trip as ONE row.
    exotic = "line1 line2 line3\x85next\x0bvtab\x0cff"
    rows = [
        corpus_row(id="greenhouse:1", source="greenhouse", description_text=exotic),
        corpus_row(id="greenhouse:2", source="greenhouse", title="Second"),
    ]
    p = tmp_path / "c.jsonl"
    write_jsonl(p, rows)
    back = read_jsonl(p)
    assert len(back) == 2
    assert back[0]["description_text"] == exotic
