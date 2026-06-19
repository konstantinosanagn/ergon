"""salary_currency must filter identically in the index and SearchQuery.matches(): a USD floor
drops EUR/GBP postings but keeps unknown-currency ones."""

from __future__ import annotations

from ergon_tracker.index.backend import SqliteIndexBackend
from ergon_tracker.index.build import build_index
from ergon_tracker.models import JobPosting, Salary, SearchQuery


def _job(sid, title, *, lo, hi, cur):
    return JobPosting.create(
        source="greenhouse",
        source_job_id=sid,
        company="Co",
        title=title,
        salary=Salary(min_amount=lo, max_amount=hi, currency=cur),
    )


def test_salary_currency_index_matches_sdk(tmp_path):
    jobs = [
        _job("1", "USD Engineer", lo=150000, hi=200000, cur="USD"),
        _job("2", "EUR Engineer", lo=150000, hi=200000, cur="EUR"),  # wrong currency -> excluded
        _job("3", "GBP Engineer", lo=150000, hi=200000, cur="GBP"),  # excluded
        _job("4", "Unknown-cur Engineer", lo=150000, hi=200000, cur=None),  # kept (unknown)
    ]
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="b1")
    q = SearchQuery(salary_min=140000, salary_currency="USD", limit=50)
    index_titles = {j.title for j in SqliteIndexBackend(p).search(q)}
    sdk_titles = {j.title for j in jobs if q.matches(j)}
    assert index_titles == sdk_titles  # parity
    assert index_titles == {"USD Engineer", "Unknown-cur Engineer"}


def test_salary_currency_lowercase_query(tmp_path):
    jobs = [
        _job("1", "USD Engineer", lo=150000, hi=200000, cur="USD"),
        _job("2", "EUR Engineer", lo=150000, hi=200000, cur="EUR"),
    ]
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="b1")
    q = SearchQuery(salary_min=140000, salary_currency="usd", limit=50)  # case-insensitive
    assert {j.title for j in SqliteIndexBackend(p).search(q)} == {"USD Engineer"}
