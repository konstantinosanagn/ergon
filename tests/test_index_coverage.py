from ergon_tracker.index.build import build_index
from ergon_tracker.index.coverage import compute_coverage, render_status_md
from ergon_tracker.index.db import connect
from ergon_tracker.models import JobLevel, JobPosting, Location, RemoteType, Salary


def _jobs():
    return [
        JobPosting.create(
            source="greenhouse",
            source_job_id="1",
            company="Stripe",
            title="Senior Backend Engineer",
            level=JobLevel.SENIOR,
            sector="Fintech",
            locations=[Location(raw="NYC", city="New York", country="US")],
            remote=RemoteType.ONSITE,
            salary=Salary(min_amount=180000, max_amount=220000, currency="USD", interval="year"),
        ),
        JobPosting.create(
            source="greenhouse",
            source_job_id="2",
            company="Stripe",
            title="Staff Engineer",
            level=JobLevel.STAFF,
            sector="Fintech",
            locations=[Location(raw="Remote", is_remote=True)],
            remote=RemoteType.REMOTE,
        ),
        JobPosting.create(
            source="lever",
            source_job_id="3",
            company="OpenAI",
            title="ML Engineer",
            level=JobLevel.MID,
            sector="AI/ML",
            locations=[Location(raw="SF", city="San Francisco", country="US")],
            remote=RemoteType.HYBRID,
        ),
    ]


def test_compute_coverage_shape(tmp_path):
    p = tmp_path / "i.sqlite"
    build_index(_jobs(), p, build_id="build-x")
    con = connect(p, read_only=True)
    cov = compute_coverage(con)

    assert cov["total_jobs"] == 3
    assert cov["active_jobs"] == 3 and cov["expired_jobs"] == 0
    assert cov["companies"] == 2
    assert cov["by_source"] == {"greenhouse": 2, "lever": 1}
    assert cov["by_sector"]["Fintech"] == 2 and cov["by_sector"]["AI/ML"] == 1
    assert cov["by_level"]["senior"] == 1 and cov["by_level"]["staff"] == 1
    assert cov["by_country"]["US"] == 2
    assert cov["remote"]["remote"] == 1 and cov["remote"]["onsite"] == 1
    assert cov["with_salary"] == 1
    assert cov["build_id"] == "build-x"
    # top_companies sorted desc by job count
    assert cov["top_companies"][0] == {"company": "Stripe", "jobs": 2}


def test_render_status_md(tmp_path):
    p = tmp_path / "i.sqlite"
    build_index(_jobs(), p, build_id="build-x")
    con = connect(p, read_only=True)
    md = render_status_md(compute_coverage(con), build_id="build-x")

    assert md.startswith("# Index Status")
    assert "build-x" in md
    assert "3" in md  # total jobs
    assert "Fintech" in md and "greenhouse" in md
    assert "Stripe" in md


def test_compute_coverage_empty(tmp_path):
    p = tmp_path / "i.sqlite"
    build_index([], p, build_id="empty")
    con = connect(p, read_only=True)
    cov = compute_coverage(con)
    assert cov["total_jobs"] == 0 and cov["companies"] == 0
    assert cov["by_source"] == {} and cov["top_companies"] == []
    # renderer must not crash on an empty index
    assert render_status_md(cov, build_id="empty").startswith("# Index Status")
