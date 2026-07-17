from __future__ import annotations

from collections import defaultdict
from typing import Any

import httpx
import pytest
import respx
from scripts.bench.crawl_corpus import (
    JD_IN_BULK_PROVIDERS,
    MIN_BOARDS_PER_PROVIDER,
    ROWS_PER_BOARD_EST,
    crawl_corpus,
    enforce_total_cap,
    per_board_caps,
    row_from_job,
    select_targets,
    trim_to_row_budget,
)

from ergon_tracker.models import (
    EmploymentType,
    JobPosting,
    Location,
    RawJob,
    RemoteType,
    Salary,
    SalaryInterval,
)

pytestmark = pytest.mark.anyio


class _StubRegistry:
    """Tiny synthetic stand-in for ``ergon_tracker.registry.store.SeedRegistry`` -- only needs
    ``.all()`` to match ``select_targets``'s structural (Protocol) contract."""

    def __init__(self, companies: dict[str, dict[str, Any]]) -> None:
        self._companies = companies

    def all(self) -> dict[str, dict[str, Any]]:
        return self._companies


def _synthetic_registry() -> _StubRegistry:
    companies: dict[str, dict[str, Any]] = {}
    for i in range(50):
        companies[f"gh{i}"] = {
            "ats": "greenhouse",
            "token": f"gh-token-{i}",
            "domain": f"gh{i}.com",
        }
    for i in range(5):
        companies[f"wk{i}"] = {"ats": "workable", "token": f"wk-token-{i}", "domain": f"wk{i}.com"}
    for i in range(3):
        companies[f"jz{i}"] = {"ats": "jazzhr", "token": f"jz-token-{i}", "domain": f"jz{i}.com"}
    # Enterprise / not-JD-in-bulk provider -- must never show up in select_targets' output, even
    # though it has plenty of candidates (this is the deliberate JD-in-bulk restriction, not a
    # "silently dropped despite being eligible" bug).
    for i in range(20):
        companies[f"ic{i}"] = {"ats": "icims", "token": f"ic-token-{i}", "domain": f"ic{i}.com"}
    return _StubRegistry(companies)


def test_select_targets_honors_floor_and_excludes_non_jd_in_bulk():
    reg = _synthetic_registry()
    out = select_targets(reg, total=30, floor=2)

    # Every JD-in-bulk provider that HAD candidates in the registry must appear -- none silently
    # dropped -- and every allocation must respect the floor (capped at real availability).
    assert set(out) == {"greenhouse", "workable", "jazzhr"}
    assert len(out["jazzhr"]) == min(3, 2) or len(out["jazzhr"]) >= 2  # floor honored / capped
    assert len(out["workable"]) >= 2
    assert len(out["greenhouse"]) >= 2

    # icims is not JD-in-bulk -- must never appear even though it had 20 candidates available.
    assert "icims" not in out

    # Total respected: sum of allocated tokens equals the budget (available exceeds it here).
    assert sum(len(v) for v in out.values()) == 30

    # Tokens are the real ATS board tokens (usable directly against provider.fetch), not the
    # internal registry company keys.
    assert all(t.startswith("gh-token-") for t in out["greenhouse"])
    assert all(t.startswith("wk-token-") for t in out["workable"])
    assert all(t.startswith("jz-token-") for t in out["jazzhr"])


def test_select_targets_no_provider_silently_dropped_when_budget_is_ample():
    reg = _synthetic_registry()
    # total far exceeds the sum of JD-in-bulk availability (50+5+3=58) -> everyone gets everything.
    out = select_targets(reg, total=1000, floor=10)
    assert out["greenhouse"] and len(out["greenhouse"]) == 50
    assert out["workable"] and len(out["workable"]) == 5
    assert out["jazzhr"] and len(out["jazzhr"]) == 3
    assert "icims" not in out


def test_select_targets_deterministic_across_calls():
    reg = _synthetic_registry()
    a = select_targets(reg, total=10, floor=1)
    b = select_targets(reg, total=10, floor=1)
    assert a == b


def test_select_targets_empty_registry_returns_empty():
    assert select_targets(_StubRegistry({}), total=100, floor=5) == {}


def _job(**overrides: Any) -> JobPosting:
    kwargs: dict[str, Any] = {
        "source": "workable",
        "source_job_id": "123",
        "company": "Acme",
        "title": "Software Engineer",
        "description_text": "Real JD body text describing the role in detail.",
        "apply_url": "https://apply.example/123",
        "employment_type": EmploymentType.CONTRACT,
        "remote": RemoteType.HYBRID,
        "locations": [Location(raw="Remote - US", city=None, country="United States")],
        "salary": Salary(
            min_amount=100000.0, max_amount=120000.0, currency="USD", interval=SalaryInterval.YEAR
        ),
    }
    kwargs.update(overrides)
    return JobPosting.create(**kwargs)


def test_row_from_job_carries_provider_stated_fields_and_jd():
    job = _job()
    row = row_from_job(job, "workable")

    assert row["id"] == "workable:123"
    assert row["source"] == "workable"
    assert row["company"] == "Acme"
    assert row["title"] == "Software Engineer"
    assert row["description_text"] == "Real JD body text describing the role in detail."
    assert row["location_raw"] == "Remote - US"
    assert row["apply_url"] == "https://apply.example/123"
    # Provider-stated, not extractor-inferred: read straight off the normalized JobPosting.
    assert row["employment_type"] == "contract"
    assert row["remote"] == "hybrid"
    assert row["structured_salary"] == {
        "min": 100000.0,
        "max": 120000.0,
        "currency": "USD",
        "interval": "year",
    }


def test_row_from_job_handles_missing_salary_and_location():
    job = _job(locations=[], salary=None, description_text="Just enough JD text to count.")
    row = row_from_job(job, "greenhouse")
    assert row["location_raw"] == ""
    assert row["structured_salary"] is None
    assert row["description_text"] == "Just enough JD text to count."


def test_row_from_job_empty_description_stays_empty_string():
    job = _job(description_text=None)
    row = row_from_job(job, "greenhouse")
    assert row["description_text"] == ""


def test_jd_in_bulk_providers_is_the_documented_list():
    assert set(JD_IN_BULK_PROVIDERS) == {
        "greenhouse",
        "ashby",
        "lever",
        "recruitee",
        "teamtailor",
        "pinpoint",
        "jazzhr",
        "dejobs",
        "join",
        "personio",
        "workable",
    }


_GREENHOUSE_FIXTURE = {
    "jobs": [
        {
            "id": 1,
            "title": "Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "content": "<p>We are looking for a backend engineer with 5+ years of experience.</p>",
            "company_name": "Acme",
            "offices": [{"name": "Remote - US"}],
            "departments": [],
            "metadata": [],
            "first_published": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
        {
            # No `content` at all -> empty description_text -> must be dropped, not written.
            "id": 2,
            "title": "Stub Posting",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
            "content": None,
            "company_name": "Acme",
            "offices": [{"name": "New York, NY"}],
            "departments": [],
            "metadata": [],
        },
    ]
}


async def test_crawl_corpus_keeps_jd_bearing_drops_empty_dedups_by_id():
    targets = {"greenhouse": ["acme"]}
    row_budget = {"greenhouse": 10}
    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(200, json=_GREENHOUSE_FIXTURE)
        )
        rows, stats = await crawl_corpus(targets, row_budget, concurrency=4)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "greenhouse:1"
    assert row["source"] == "greenhouse"
    assert row["description_text"]
    assert stats["greenhouse"]["kept"] == 1
    assert stats["greenhouse"]["empty_description"] == 1


async def test_crawl_corpus_unknown_provider_is_counted_not_silently_dropped():
    targets = {"__not_a_real_provider__": ["token1"]}
    rows, stats = await crawl_corpus(targets, {}, concurrency=4)
    assert rows == []
    assert stats["__not_a_real_provider__"]["provider_missing"] == 1


def test_module_constants_match_documented_defaults():
    # THE BUG this module fixes: --total/--floor used to bound BOARDS (then keep every job on
    # each board), so a --total 90 smoke run produced 15,359 rows. These constants size a BOARD
    # budget generously off the ROW target instead, so the row-budgeting logic below (per-board
    # caps + deterministic trim) is what actually enforces --total as a row count.
    assert ROWS_PER_BOARD_EST == 40
    assert MIN_BOARDS_PER_PROVIDER == 3


def test_per_board_caps_splits_row_budget_evenly_across_a_providers_boards():
    targets = {"greenhouse": ["a", "b", "c"], "workable": ["x"]}
    row_budget = {"greenhouse": 100, "workable": 7}
    caps = per_board_caps(targets, row_budget)
    assert caps["greenhouse"] == 34  # ceil(100 / 3)
    assert caps["workable"] == 7  # ceil(7 / 1)


def test_per_board_caps_missing_budget_floors_at_one_not_zero():
    # A provider with no row_budget entry must still get boards a nonzero (if pointless) cap --
    # never a cap of 0, which would silently starve every board of that provider.
    targets = {"jazzhr": ["a", "b"]}
    caps = per_board_caps(targets, {})
    assert caps["jazzhr"] == 1


def test_trim_to_row_budget_keeps_first_n_by_id_and_counts_the_rest_as_trimmed():
    rows = {f"greenhouse:{i}": {"id": f"greenhouse:{i}", "source": "greenhouse"} for i in range(10)}
    stats: dict[str, dict[str, int]] = {"greenhouse": defaultdict(int)}
    kept = trim_to_row_budget(rows, {"greenhouse": 4}, stats)
    assert len(kept) == 4
    assert [r["id"] for r in kept] == [
        "greenhouse:0",
        "greenhouse:1",
        "greenhouse:2",
        "greenhouse:3",
    ]
    assert stats["greenhouse"]["trimmed"] == 6


def test_trim_to_row_budget_is_a_noop_when_under_budget():
    rows = {"greenhouse:0": {"id": "greenhouse:0", "source": "greenhouse"}}
    stats: dict[str, dict[str, int]] = {"greenhouse": defaultdict(int)}
    kept = trim_to_row_budget(rows, {"greenhouse": 100}, stats)
    assert kept == [{"id": "greenhouse:0", "source": "greenhouse"}]
    assert stats["greenhouse"]["trimmed"] == 0


def test_enforce_total_cap_trims_across_providers_deterministically():
    rows = [{"id": f"src:{i}", "source": "src"} for i in range(10)]
    stats: dict[str, dict[str, int]] = {"src": defaultdict(int)}
    kept = enforce_total_cap(rows, 3, stats)
    assert len(kept) == 3
    assert [r["id"] for r in kept] == ["src:0", "src:1", "src:2"]
    assert stats["src"]["trimmed"] == 7


def test_enforce_total_cap_is_a_noop_when_under_total():
    rows = [{"id": "a", "source": "src"}]
    stats: dict[str, dict[str, int]] = {"src": defaultdict(int)}
    kept = enforce_total_cap(rows, 10, stats)
    assert kept == rows
    assert stats["src"]["trimmed"] == 0


class _StubProvider:
    """A fake provider whose ``.fetch`` returns synthetic ``RawJob``s (no network) and whose
    ``.normalize`` returns synthetic ``JobPosting``s carrying real description text, so
    ``_crawl_one``/``crawl_corpus`` can be exercised end-to-end without ``respx`` or real ATS
    fixtures. Job ids are namespaced by ``token`` so postings from different boards never
    collide on id (mirrors real ATS behavior: two different boards never share a job id)."""

    def __init__(self, n_jobs: int) -> None:
        self.n_jobs = n_jobs

    async def fetch(self, token: str, query: Any, fetcher: Any) -> list[RawJob]:
        return [
            RawJob(source="stub", source_job_id=f"{token}-{i}", company="Acme", token=token)
            for i in range(self.n_jobs)
        ]

    def normalize(self, raw: RawJob) -> JobPosting:
        return JobPosting.create(
            source="stub",
            source_job_id=raw.source_job_id,
            company=raw.company,
            title="Engineer",
            description_text="Real synthetic JD body text describing the role.",
            apply_url=f"https://apply.example/{raw.source_job_id}",
            employment_type=EmploymentType.FULL_TIME,
            remote=RemoteType.REMOTE,
            locations=[],
            salary=None,
        )


async def test_crawl_corpus_per_board_cap_bounds_a_single_boards_contribution(monkeypatch):
    # A board that WOULD yield 1000 jobs must be capped at its row budget, not "every job on the
    # board" -- this is the exact shape of the bug (dejobs: 12 boards -> 13,541 rows).
    provider = _StubProvider(n_jobs=1000)
    monkeypatch.setattr("scripts.bench.crawl_corpus.load_builtins", lambda: None)
    monkeypatch.setattr("scripts.bench.crawl_corpus.get_provider", lambda ats: provider)

    targets = {"greenhouse": ["acme"]}
    row_budget = {"greenhouse": 50}
    rows, stats = await crawl_corpus(targets, row_budget, concurrency=4)

    assert len(rows) <= 50
    assert stats["greenhouse"]["kept"] <= 50
    assert all(r["source"] == "greenhouse" for r in rows)


async def test_crawl_corpus_trims_provider_total_to_row_budget_after_concurrent_overshoot(
    monkeypatch,
):
    # 3 boards each independently capped at ceil(10/3)=4 can together keep 12 > the provider's
    # row_budget of 10 -- the deterministic post-fetch trim must bring the provider back down to
    # exactly its budget, not leave the overshoot in the output.
    provider = _StubProvider(n_jobs=1000)
    monkeypatch.setattr("scripts.bench.crawl_corpus.load_builtins", lambda: None)
    monkeypatch.setattr("scripts.bench.crawl_corpus.get_provider", lambda ats: provider)

    targets = {"greenhouse": ["board-1", "board-2", "board-3"]}
    row_budget = {"greenhouse": 10}
    rows, stats = await crawl_corpus(targets, row_budget, concurrency=4)

    assert len(rows) == 10
    assert stats["greenhouse"]["trimmed"] == 2


async def test_crawl_corpus_enforces_overall_total_across_providers(monkeypatch):
    provider = _StubProvider(n_jobs=1000)
    monkeypatch.setattr("scripts.bench.crawl_corpus.load_builtins", lambda: None)
    monkeypatch.setattr("scripts.bench.crawl_corpus.get_provider", lambda ats: provider)

    targets = {"greenhouse": ["gh-board"], "workable": ["wk-board"]}
    row_budget = {"greenhouse": 30, "workable": 30}
    rows, stats = await crawl_corpus(targets, row_budget, total=20, concurrency=4)

    assert len(rows) == 20
    assert stats["greenhouse"]["trimmed"] + stats["workable"]["trimmed"] > 0
