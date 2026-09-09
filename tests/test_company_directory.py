"""Tests for the active/dormant company directory (registry x index join)."""

from __future__ import annotations

from ergon.index.build import build_index
from ergon.index.coverage import company_directory
from ergon.index.db import connect
from ergon.models import JobPosting


def _index_with(tmp_path, jobs):
    db = tmp_path / "index.sqlite"
    build_index(jobs, db, build_id="b1")
    return connect(db)


def test_active_dormant_split_and_counts(tmp_path):
    # Index has postings for Acme (2) and Globex (1); registry also lists Dormant Co (no jobs).
    jobs = [
        JobPosting.create(source="greenhouse", source_job_id="1", company="Acme", title="Eng"),
        JobPosting.create(source="greenhouse", source_job_id="2", company="Acme", title="PM"),
        JobPosting.create(source="lever", source_job_id="3", company="Globex", title="Eng"),
    ]
    con = _index_with(tmp_path, jobs)
    registry = {
        "acme": {"ats": "greenhouse", "token": "acme", "domain": "acme.com"},
        "globex": {"ats": "lever", "token": "globex", "domain": None},
        "dormant-co": {"ats": "ashby", "token": "dormantco", "domain": None},
    }
    try:
        d = company_directory(con, registry)
    finally:
        con.close()

    assert d["registered"] == 3
    assert d["active"] == 2
    assert d["dormant"] == 1
    by = {c["company"]: c for c in d["companies"]}
    assert by["acme"]["status"] == "active" and by["acme"]["open_roles"] == 2
    assert by["globex"]["status"] == "active" and by["globex"]["open_roles"] == 1
    assert by["dormant-co"]["status"] == "dormant" and by["dormant-co"]["open_roles"] == 0
    # sorted by open_roles desc
    assert [c["company"] for c in d["companies"]][:2] == ["acme", "globex"]


def test_join_normalizes_slug_vs_company_name(tmp_path):
    # Registry slug "palantir-technologies" must match index company_key "palantir technologies".
    jobs = [
        JobPosting.create(
            source="lever", source_job_id="1", company="Palantir Technologies", title="FDSE"
        )
    ]
    con = _index_with(tmp_path, jobs)
    registry = {"palantir-technologies": {"ats": "lever", "token": "palantir", "domain": None}}
    try:
        d = company_directory(con, registry)
    finally:
        con.close()
    assert d["active"] == 1 and d["companies"][0]["open_roles"] == 1


def test_status_and_query_filters_do_not_change_totals(tmp_path):
    jobs = [
        JobPosting.create(source="greenhouse", source_job_id="1", company="Acme", title="Eng"),
    ]
    con = _index_with(tmp_path, jobs)
    registry = {
        "acme": {"ats": "greenhouse", "token": "acme", "domain": None},
        "beta": {"ats": "lever", "token": "beta", "domain": None},
    }
    try:
        only_dormant = company_directory(con, registry, status="dormant")
        only_acme = company_directory(con, registry, query="acm")
    finally:
        con.close()
    # totals are full regardless of filters
    assert only_dormant["active"] == 1 and only_dormant["registered"] == 2
    assert [c["company"] for c in only_dormant["companies"]] == ["beta"]
    assert [c["company"] for c in only_acme["companies"]] == ["acme"]


def test_index_only_company_not_in_registry(tmp_path):
    jobs = [
        JobPosting.create(source="remoteok", source_job_id="1", company="AggOnly", title="Eng"),
    ]
    con = _index_with(tmp_path, jobs)
    registry = {"acme": {"ats": "greenhouse", "token": "acme", "domain": None}}
    try:
        d = company_directory(con, registry)
    finally:
        con.close()
    assert d["index_only"] == 1  # AggOnly has postings but isn't a registered board
    assert d["active"] == 0 and d["dormant"] == 1
