"""Three defects found reviewing the content-revalidation and alias changes, pinned here.

1. The content stamp advanced only when EVERY raw on a board normalized. One permanently
   malformed posting therefore left its board unstamped forever: due every day, fetched every day
   with ``etag=None`` — conditional caching and delta-skip both permanently disabled for it.

2. Legacy state has no stamp, so every eligible board (47,779 of 54,208, including all 19,898
   join.com boards, which have no conditional URL and whose only cheap path IS delta-skip) was due
   on the first day after deploy and again, together, every seventh day. The first revalidation is
   now staggered by a stable hash of the board key.

3. The ML alias guard stopped an expanded variant from crossing the five-token any-word threshold,
   but for a query already at five tokens the any-word bag applied to the expanded variant too,
   admitting "Machine Operator" and "Learning & Development Specialist" as candidates for
   "ML infra platform engineer remote". An expanded variant never gets the bag.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta

import anyio
from tests.test_delta_crawl_skip import _POSTINGS, _Provider, _Reg, _write_sidecar, bi, idset_hash

from ergon.index.build import build_index
from ergon.index.query import search_rows
from ergon.index.scheduler import COLD_INTERVAL, BoardState, content_crawl_due
from ergon.models import JobPosting, Location, SearchQuery


class _OneBadRaw(_Provider):
    calls = 0

    def normalize(self, raw):  # noqa: ANN001, ANN201
        _OneBadRaw.calls += 1
        if _OneBadRaw.calls == 1:
            raise ValueError("this one posting never parses")
        return super().normalize(raw)


def test_one_malformed_posting_does_not_disable_a_boards_caching(monkeypatch, tmp_path) -> None:
    import ergon.providers.base as base_mod
    import ergon.registry.store as store_mod

    prov = _OneBadRaw()
    monkeypatch.setattr(store_mod, "SeedRegistry", _Reg)
    monkeypatch.setattr(base_mod, "get_provider", lambda n: prov)
    monkeypatch.setattr(base_mod, "load_builtins", lambda: None)
    monkeypatch.setattr(bi, "_today", lambda: "2026-09-10")
    monkeypatch.setenv("ERGON_DELTA_CRAWL", "1")
    fingerprint = idset_hash({p[0] for p in _POSTINGS})
    _write_sidecar(tmp_path / "index-freshness.sqlite", "greenhouse", "acme", fingerprint)
    state = BoardState(provider="greenhouse", token="acme", last_content_crawled="2026-09-01")
    out, _ = anyio.run(bi._crawl_due, 10, {state.key: state}, tmp_path / "fresh.sqlite", "eval")
    assert _OneBadRaw.calls >= 2 and not out[state.key].get("error")
    assert state.last_content_crawled == "2026-09-10", "a successful crawl must stamp the board"
    assert not content_crawl_due(state, "2026-09-11")


def test_first_revalidation_of_legacy_state_is_spread_across_the_interval() -> None:
    boards = [BoardState(provider="join", token=f"co-{i}") for i in range(2000)]
    d0 = date(2026, 9, 11)
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(COLD_INTERVAL)]
    per_day = Counter(d for b in boards for d in days if content_crawl_due(b, d))
    assert len(per_day) == COLD_INTERVAL, "every day of the interval must carry some of the load"
    assert max(per_day.values()) < len(boards) * 0.25, f"one-day herd: {per_day}"
    for b in boards:
        assert sum(content_crawl_due(b, d) for d in days) == 1, "due exactly once per interval"


def test_stagger_is_stable_across_processes() -> None:
    """A per-process hash seed would move a board's day every run and defeat the spread.

    Fifty keys: a salted hash agrees with the stable one on a single key one time in seven.
    """
    import hashlib

    days = [date(2026, 9, 11) + timedelta(days=i) for i in range(COLD_INTERVAL)]
    for i in range(50):
        b = BoardState(provider="join", token=f"co-{i}")
        due = [content_crawl_due(b, d.isoformat()) for d in days]
        slot = int(hashlib.sha1(b.key.encode()).hexdigest(), 16) % COLD_INTERVAL
        assert due.index(True) == next(
            j for j, d in enumerate(days) if d.toordinal() % COLD_INTERVAL == slot
        )


def test_alias_expansion_never_gets_the_any_word_bag(tmp_path) -> None:
    titles = [
        "Machine Operator",
        "Learning & Development Specialist",
        "ML Platform Engineer",
        "Sewing Machine Mechanic",
        "Remote Customer Support",
    ]
    jobs = [
        JobPosting.create(
            source="greenhouse",
            source_job_id=f"j{i}",
            company=f"Co {i}",
            title=t,
            locations=[Location(raw="US", country="US")],
        )
        for i, t in enumerate(titles)
    ]
    p = tmp_path / "i.sqlite"
    build_index(jobs, p, build_id="b")
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    got = {
        r["title"]
        for r in search_rows(
            con, SearchQuery(keywords="ML infra platform engineer remote", semantic=True, limit=20)
        )
    }
    assert "ML Platform Engineer" in got
    assert not got & {
        "Machine Operator",
        "Sewing Machine Mechanic",
        "Learning & Development Specialist",
    }
