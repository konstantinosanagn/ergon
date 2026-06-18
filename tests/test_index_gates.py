from ergon_tracker.index.build import build_index
from ergon_tracker.index.gates import evaluate_gates
from ergon_tracker.models import JobLevel, JobPosting, Location, RemoteType


def _job(sid, company, title, **kw):
    return JobPosting.create(
        source="greenhouse", source_job_id=sid, company=company, title=title,
        locations=[Location(raw="Remote", is_remote=True)], remote=RemoteType.REMOTE, **kw,
    )


def _db(tmp_path, jobs, name="i.sqlite"):
    p = tmp_path / name
    build_index(jobs, p, build_id="b1")
    return p


def test_gates_pass_on_healthy_cold_start(tmp_path):
    p = _db(tmp_path, [_job("1", "Stripe", "Backend Engineer", level=JobLevel.SENIOR),
                       _job("2", "Ramp", "Frontend Engineer")])
    rep = evaluate_gates(p)
    assert rep.passed, rep.summary()


def test_gates_fail_on_row_collapse_vs_prev(tmp_path):
    p = _db(tmp_path, [_job("1", "Stripe", "Backend Engineer")])
    rep = evaluate_gates(p, prev_row_count=100)  # 1 row vs prev 100 -> below 75% floor
    assert not rep.passed
    assert any(r.name == "row_floor" and not r.passed for r in rep.results)


def test_gates_pass_when_within_floor(tmp_path):
    jobs = [_job(str(i), f"Co{i}", f"Engineer {i}") for i in range(10)]
    p = _db(tmp_path, jobs)
    rep = evaluate_gates(p, prev_row_count=11)  # 10 >= floor(8)
    assert rep.passed, rep.summary()


def test_gates_fail_on_empty_cold_start(tmp_path):
    p = _db(tmp_path, [])
    rep = evaluate_gates(p)
    assert not rep.passed
    assert any(r.name == "row_floor" and not r.passed for r in rep.results)


def test_report_to_dict_shape(tmp_path):
    p = _db(tmp_path, [_job("1", "Stripe", "Eng")])
    d = evaluate_gates(p).to_dict()
    assert "passed" in d and isinstance(d["gates"], list)
    assert {"integrity_check", "schema_version", "row_floor", "no_duplicate_ids",
            "company_fk_intact"} == {g["name"] for g in d["gates"]}
