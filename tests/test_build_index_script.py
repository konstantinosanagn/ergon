import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_index import publish_artifacts  # noqa: E402

from ergon_tracker.index.build import build_index  # noqa: E402
from ergon_tracker.models import JobPosting  # noqa: E402


def test_publish_writes_gz_and_manifest(tmp_path):
    src = tmp_path / "i.sqlite"
    build_index(
        [JobPosting.create(source="greenhouse", source_job_id="1", company="Co", title="Eng")],
        src,
        build_id="b1",
    )
    out = tmp_path / "dist"
    publish_artifacts(src, out, build_id="b1")
    man = json.loads((out / "manifest.json").read_text())
    assert man["build_id"] == "b1" and man["schema_version"] == 1
    raw = gzip.decompress((out / "index.sqlite.gz").read_bytes())
    assert hashlib.sha256(raw).hexdigest() == man["sha256"]


def test_append_history_accumulates(tmp_path):
    from build_index import append_history

    h = tmp_path / "runs" / "history.jsonl"
    append_history(h, {"build_id": "b1", "total_jobs": 10})
    append_history(h, {"build_id": "b2", "total_jobs": 12})
    import json

    rows = [json.loads(line) for line in h.read_text().splitlines()]
    assert [r["build_id"] for r in rows] == ["b1", "b2"]


def test_build_and_publish_shards_gzips(tmp_path):
    from build_index import build_and_publish_shards

    from ergon_tracker.models import JobPosting

    jobs = [
        JobPosting.create(
            source="greenhouse",
            source_job_id=str(i),
            company=f"Co{i}",
            title="Engineer",
            sector=("Fintech" if i % 2 else None),
        )
        for i in range(4)
    ]
    n = build_and_publish_shards(jobs, tmp_path, build_id="b1")
    assert n == 2  # fintech + unknown
    assert (tmp_path / "shards.json").exists()
    gzs = {f.name for f in tmp_path.glob("shard-*.sqlite.gz")}
    assert gzs == {"shard-fintech.sqlite.gz", "shard-unknown.sqlite.gz"}
