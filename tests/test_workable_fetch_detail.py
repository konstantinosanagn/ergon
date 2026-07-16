"""Tier-3 detail fetcher: WorkableProvider.fetch_detail (67,199 Workable postings, list-only).

Offline only — a FakeFetcher stands in for AsyncFetcher; no live network calls. Mirrors
``tests/test_workday_fetch_detail.py``.

Board-bulk memoization (the throughput win): the bulk widget endpoint
(``GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true``) returns EVERY
job on a board WITH a full ``description`` in ONE call, so :meth:`WorkableProvider.fetch_detail`
fetches a board at most once per run and serves every sibling posting on it from a module-level
memo cache (``_desc_by_shortcode``) — zero network calls for cache hits. Workable's index
``apply_url`` is a BARE shortlink, ``https://apply.workable.com/j/{shortcode}`` (no account slug
embedded), so resolving the slug on a cache miss is one redirect-disabled GET on the shortlink
whose ``Location`` header reveals it (``/{slug}/j/{shortcode}``). The fake fetcher below
simulates both the redirect hop and the board-bulk fetch via response maps keyed by URL, and
counts calls per URL so tests can assert a board is fetched AT MOST ONCE."""

from __future__ import annotations

import anyio
import httpx
import pytest

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.providers.base import BaseProvider
from ergon_tracker.providers.workable import (
    _MAX_BOARD_FETCH_ATTEMPTS,
    WorkableProvider,
    _reset_workable_cache,
)

_SHORTLINK = "https://apply.workable.com/j/516863E6FD"
_SHORTLINK_2 = "https://apply.workable.com/j/AAAAAAAAAA"
_REDIRECT_TARGET = "https://apply.workable.com/jobrack/j/516863E6FD"
_BOARD_URL = "https://apply.workable.com/api/v1/widget/accounts/jobrack"


@pytest.fixture(autouse=True)
def _clean_workable_cache():
    """The detail memo cache is module-level by design (persists across a whole reconcile run),
    so tests must reset it on both sides to stay isolated from each other."""
    _reset_workable_cache()
    yield
    _reset_workable_cache()


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_body: object = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}

    def json(self) -> object:
        return self._json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]


class _FakeFetcher:
    """Simulates the redirect hop (``request``) and the board-bulk fetch (``get_json``).

    ``redirects`` maps a shortlink URL -> the ``Location`` header value returned by a
    redirect-disabled GET (``None`` means a 301 with no ``Location`` header at all).
    ``board_payloads`` maps a board URL -> the JSON body ``get_json`` returns for it.
    ``board_raises`` is the set of board URLs whose ``get_json`` call ALWAYS raises (a
    permanently-dead board). ``board_fail_first_n`` maps a board URL -> a number of LEADING
    calls that raise before subsequent calls succeed (a transiently-flaky board that recovers).
    Any URL not explicitly wired raises ``AssertionError``, so an unexpected extra call
    (e.g. a second fetch of an already-cached board) fails the test loudly."""

    def __init__(
        self,
        *,
        redirects: dict[str, str | None] | None = None,
        board_payloads: dict[str, object] | None = None,
        board_raises: frozenset[str] = frozenset(),
        board_fail_first_n: dict[str, int] | None = None,
    ) -> None:
        self._redirects = redirects or {}
        self._board_payloads = board_payloads or {}
        self._board_raises = board_raises
        self._board_fail_first_n = board_fail_first_n or {}
        self.calls: list[tuple[str, str]] = []
        self.board_fetch_count: dict[str, int] = {}

    async def request(self, method: str, url: str, **kw: object) -> _FakeResponse:
        self.calls.append(("REQUEST", url))
        if url in self._redirects:
            loc = self._redirects[url]
            headers = {} if loc is None else {"location": loc}
            return _FakeResponse(status_code=301, headers=headers)
        raise AssertionError(f"unexpected request() call: {method} {url}")

    async def get_json(self, url: str, **kw: object) -> object:
        self.calls.append(("GET_JSON", url))
        # A real network call always yields control back to the event loop before its result
        # is available; a purely-synchronous fake would mask the board-cache race (concurrent
        # siblings would never get a chance to interleave). This checkpoint makes the fake
        # behave like the real thing for concurrency tests.
        await anyio.sleep(0)
        if url in self._board_payloads:
            self.board_fetch_count[url] = self.board_fetch_count.get(url, 0) + 1
            if url in self._board_raises:
                raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]
            fail_n = self._board_fail_first_n.get(url, 0)
            if self.board_fetch_count[url] <= fail_n:
                raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]
            return self._board_payloads[url]
        raise AssertionError(f"unexpected get_json() call: {url}")


def _job(shortcode: str, description: str = "<p>Full JD text.</p>", **extra: object) -> dict:
    payload: dict[str, object] = {"shortcode": shortcode, "description": description}
    payload.update(extra)
    return payload


def _board_payload(*jobs: dict) -> dict:
    return {"name": "Jobrack", "jobs": list(jobs)}


def _ref(
    *,
    apply_url: str | None,
    token: str | None = None,
    listing_url: str | None = None,
    id_: str = "1",
) -> DetailRef:
    return DetailRef(
        id=id_,
        source="workable",
        token=token,
        apply_url=apply_url,
        listing_url=listing_url,
        content_sig="s",
    )


def test_workable_fetch_detail_board_bulk_returns_description() -> None:
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={
            _BOARD_URL: _board_payload(
                _job("516863E6FD", "<p>Real JD text for the jobrack posting...</p>")
            )
        },
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc == "<p>Real JD text for the jobrack posting...</p>"
    assert fetcher.calls == [
        ("REQUEST", _SHORTLINK),
        ("GET_JSON", _BOARD_URL),
    ]


def test_workable_fetch_detail_cache_hit_two_postings_same_board_one_board_fetch() -> None:
    # Two DIFFERENT postings on the SAME board -> exactly ONE board fetch total; the second
    # posting is served purely from the memo cache primed by the first (siblings win).
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={
            _BOARD_URL: _board_payload(
                _job("516863E6FD", "<p>First posting JD.</p>"),
                _job("AAAAAAAAAA", "<p>Second posting JD.</p>"),
            )
        },
    )
    ref1 = _ref(apply_url=_SHORTLINK, id_="1")
    ref2 = _ref(apply_url=_SHORTLINK_2, id_="2")

    desc1 = anyio.run(lambda: WorkableProvider().fetch_detail(ref1, fetcher))
    assert desc1 == "<p>First posting JD.</p>"
    assert fetcher.board_fetch_count[_BOARD_URL] == 1

    # Second posting: shares the SAME shortlink host but a different shortcode not yet resolved
    # directly -> must still hit the cache (primed as a sibling), not the network, since its
    # board slug can't even be re-resolved here (no redirect wired for _SHORTLINK_2).
    desc2 = anyio.run(lambda: WorkableProvider().fetch_detail(ref2, fetcher))
    assert desc2 == "<p>Second posting JD.</p>"

    # Still exactly one board fetch, and no redirect hop for the second posting.
    assert fetcher.board_fetch_count[_BOARD_URL] == 1
    assert fetcher.calls == [
        ("REQUEST", _SHORTLINK),
        ("GET_JSON", _BOARD_URL),
    ]


def test_workable_fetch_detail_repeat_call_same_posting_is_cache_hit() -> None:
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={_BOARD_URL: _board_payload(_job("516863E6FD", "<p>JD.</p>"))},
    )
    ref = _ref(apply_url=_SHORTLINK)

    first = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))
    second = anyio.run(lambda: WorkableProvider().fetch_detail(ref, fetcher))

    assert first == second == "<p>JD.</p>"
    assert fetcher.board_fetch_count[_BOARD_URL] == 1
    # The second call makes NO network calls at all.
    assert fetcher.calls == [
        ("REQUEST", _SHORTLINK),
        ("GET_JSON", _BOARD_URL),
    ]


def test_workable_fetch_detail_board_bulk_concatenates_requirements_and_benefits() -> None:
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={
            _BOARD_URL: _board_payload(
                _job(
                    "516863E6FD",
                    "<p>Description.</p>",
                    requirements="<p>Requirements.</p>",
                    benefits="<p>Benefits.</p>",
                )
            )
        },
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc == "<p>Description.</p>\n<p>Requirements.</p>\n<p>Benefits.</p>"


def test_workable_fetch_detail_skips_redirect_when_url_already_full() -> None:
    # apply_url already embeds the slug (post-redirect / full shape) -> no redirect hop needed.
    fetcher = _FakeFetcher(
        board_payloads={_BOARD_URL: _board_payload(_job("516863E6FD", "<p>Already full.</p>"))}
    )
    desc = anyio.run(
        lambda: WorkableProvider().fetch_detail(_ref(apply_url=_REDIRECT_TARGET), fetcher)
    )
    assert desc == "<p>Already full.</p>"
    assert fetcher.calls == [("GET_JSON", _BOARD_URL)]


def test_workable_fetch_detail_uses_ref_token_when_present() -> None:
    # ref.token set (a future board_token backfill) -> skip the redirect hop entirely.
    fetcher = _FakeFetcher(
        board_payloads={_BOARD_URL: _board_payload(_job("516863E6FD", "<p>Via ref.token.</p>"))}
    )
    desc = anyio.run(
        lambda: WorkableProvider().fetch_detail(
            _ref(apply_url=_SHORTLINK, token="jobrack"), fetcher
        )
    )
    assert desc == "<p>Via ref.token.</p>"
    assert fetcher.calls == [("GET_JSON", _BOARD_URL)]


def test_workable_fetch_detail_falls_back_to_listing_url() -> None:
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={
            _BOARD_URL: _board_payload(_job("516863E6FD", "<p>Fallback JD via listing_url...</p>"))
        },
    )
    desc = anyio.run(
        lambda: WorkableProvider().fetch_detail(
            _ref(apply_url=None, listing_url=_SHORTLINK), fetcher
        )
    )
    assert desc == "<p>Fallback JD via listing_url...</p>"


def test_workable_fetch_detail_missing_description_is_none() -> None:
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={_BOARD_URL: _board_payload({"shortcode": "516863E6FD"})},
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc is None


def test_workable_fetch_detail_empty_description_is_none() -> None:
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={_BOARD_URL: _board_payload(_job("516863E6FD", "   "))},
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc is None


def test_workable_fetch_detail_non_dict_board_payload_is_none() -> None:
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={_BOARD_URL: ["not", "a", "dict"]},
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc is None


def test_workable_fetch_detail_non_list_jobs_is_none() -> None:
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={_BOARD_URL: {"name": "Jobrack", "jobs": "not-a-list"}},
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc is None


def test_workable_fetch_detail_non_str_description_is_none() -> None:
    # ``description`` truthy but not a string must not raise (the SmartRecruiters regression).
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={
            _BOARD_URL: _board_payload(
                {"shortcode": "516863E6FD", "description": {"nested": "not-a-string"}}
            )
        },
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc is None


def test_workable_fetch_detail_shortcode_absent_from_board_is_none() -> None:
    # This posting's shortcode never appears in its own board's bulk response (e.g. removed) ->
    # None, and the board is still only fetched once (tracked via _fetched_board_slugs).
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={_BOARD_URL: _board_payload(_job("SOME-OTHER-CODE", "<p>Other.</p>"))},
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc is None
    assert fetcher.board_fetch_count[_BOARD_URL] == 1


def test_workable_fetch_detail_board_fetch_failure_is_none() -> None:
    fetcher = _FakeFetcher(
        redirects={_SHORTLINK: "/jobrack/j/516863E6FD"},
        board_payloads={_BOARD_URL: _board_payload(_job("516863E6FD", "<p>JD.</p>"))},
        board_raises=frozenset({_BOARD_URL}),
    )
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc is None


def test_workable_fetch_detail_missing_redirect_location_is_none() -> None:
    # Redirect hop returns no Location header -> slug can't be resolved -> None, never raises.
    fetcher = _FakeFetcher(redirects={_SHORTLINK: None})
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=_SHORTLINK), fetcher))
    assert desc is None


def test_workable_fetch_detail_unparseable_url_is_none() -> None:
    fetcher = _FakeFetcher()
    desc = anyio.run(
        lambda: WorkableProvider().fetch_detail(
            _ref(apply_url="https://example.com/not-a-workable-url"), fetcher
        )
    )
    assert desc is None


def test_workable_fetch_detail_no_urls_is_none() -> None:
    fetcher = _FakeFetcher()
    desc = anyio.run(lambda: WorkableProvider().fetch_detail(_ref(apply_url=None), fetcher))
    assert desc is None


def test_base_fetch_detail_is_none() -> None:
    ref = DetailRef(
        id="1", source="x", token=None, apply_url=None, listing_url=None, content_sig="s"
    )
    desc = anyio.run(lambda: BaseProvider().fetch_detail(ref, _FakeFetcher()))
    assert desc is None


def test_workable_fetch_detail_concurrent_same_board_siblings_await_not_race() -> None:
    """Regression for the board-cache check-then-act RACE: N concurrent ``fetch_detail`` calls
    for DISTINCT postings on the SAME board, fired via a single ``anyio.create_task_group``
    (mirroring how ``detail.py::_run_pipeline`` drives real concurrency). Each posting's
    ``apply_url`` already embeds the slug (the "full shortlink" shape), so no redirect hop is
    needed -- every task races straight into the board-resolution path together.

    Before the per-slug lock: the first task to run marks the board "fetched" (a synchronous
    ``_fetched_board_slugs.add(slug)`` at the very start of ``_fetch_board``) and only THEN
    awaits the actual board fetch (``fetcher.get_json``, which yields via the fake's
    ``anyio.sleep(0)`` checkpoint). Every sibling that gets scheduled during that window sees
    the board "already handled", skips fetching, and reads the (still-empty)
    ``_desc_by_shortcode`` -> ``None``, permanently, for the rest of the run. Confirmed: with
    the lock reverted, only 1 of N postings resolves a description.

    After the fix: siblings block on the per-slug ``anyio.Lock`` instead of racing, so exactly
    ONE board fetch happens and every posting resolves its description."""
    n = 15
    slug = "racedboard"
    board_url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    jobs = [_job(f"CODE{i:02d}", f"<p>JD {i}</p>") for i in range(n)]
    fetcher = _FakeFetcher(board_payloads={board_url: _board_payload(*jobs)})
    refs = [
        _ref(apply_url=f"https://apply.workable.com/{slug}/j/CODE{i:02d}", id_=str(i))
        for i in range(n)
    ]
    results: list[str | None] = [None] * n

    async def _worker(i: int) -> None:
        results[i] = await WorkableProvider().fetch_detail(refs[i], fetcher)

    async def _run() -> None:
        async with anyio.create_task_group() as tg:
            for i in range(n):
                tg.start_soon(_worker, i)

    anyio.run(_run)

    assert results == [f"<p>JD {i}</p>" for i in range(n)]
    assert fetcher.board_fetch_count[board_url] == 1


# --- poison-pill regression: a failed board fetch must not permanently kill every sibling ------
# Bug: ``_fetch_board`` used to add the slug to ``_fetched_board_slugs`` UNCONDITIONALLY before
# the bulk fetch even started, then swallow the exception -- so ONE dropped board fetch left the
# slug marked "done" with an empty cache, and every sibling posting on that board returned
# ``None`` for the rest of the run (one failed request killing ~1,100 postings on a large board).
# Fix: only mark the slug fetched on SUCCESS; a failure increments a bounded per-slug retry
# counter (``_board_fail_counts`` / ``_MAX_BOARD_FETCH_ATTEMPTS``) instead, so a later sibling
# posting gets a chance to retry -- but a genuinely-dead board still stops being re-hammered
# after a few attempts.


def test_workable_fetch_detail_board_fails_once_then_recovers_for_siblings() -> None:
    slug = "flaky"
    board_url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    fetcher = _FakeFetcher(
        board_payloads={
            board_url: _board_payload(
                _job("CODE01", "<p>First posting JD.</p>"),
                _job("CODE02", "<p>Second posting JD.</p>"),
            )
        },
        board_fail_first_n={board_url: 1},  # the FIRST bulk fetch attempt fails; the 2nd works
    )
    ref1 = _ref(apply_url=f"https://apply.workable.com/{slug}/j/CODE01", id_="1")
    ref2 = _ref(apply_url=f"https://apply.workable.com/{slug}/j/CODE02", id_="2")

    # This posting triggers the board's first (failing) bulk fetch -> None, but the board must
    # NOT be poisoned as "fetched" -- a sibling gets to retry it.
    desc1 = anyio.run(lambda: WorkableProvider().fetch_detail(ref1, fetcher))
    assert desc1 is None

    # A sibling posting on the SAME board retries the fetch -> succeeds this time -> recovers.
    desc2 = anyio.run(lambda: WorkableProvider().fetch_detail(ref2, fetcher))
    assert desc2 == "<p>Second posting JD.</p>"

    # The FIRST posting's shortcode is now populated too (primed by the successful retry) --
    # proving it wasn't permanently poisoned by the earlier failed attempt.
    desc1_again = anyio.run(lambda: WorkableProvider().fetch_detail(ref1, fetcher))
    assert desc1_again == "<p>First posting JD.</p>"

    assert fetcher.board_fetch_count[board_url] == 2  # one failed attempt + one successful retry


def test_workable_fetch_detail_board_always_fails_bounded_reattempts_not_infinite() -> None:
    slug = "deadboard"
    board_url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    fetcher = _FakeFetcher(
        board_payloads={board_url: _board_payload(_job("CODE00", "<p>Unreachable.</p>"))},
        board_raises=frozenset({board_url}),
    )
    # More siblings than the retry budget, each on the SAME permanently-dead board.
    refs = [
        _ref(apply_url=f"https://apply.workable.com/{slug}/j/CODE{i:02d}", id_=str(i))
        for i in range(_MAX_BOARD_FETCH_ATTEMPTS + 3)
    ]

    for ref in refs:
        desc = anyio.run(lambda r=ref: WorkableProvider().fetch_detail(r, fetcher))
        assert desc is None

    # Every sibling gets None, but the board is only ever re-attempted up to the bound -- not
    # once per sibling -- so it's treated as known-failed and stops being re-hammered.
    assert fetcher.board_fetch_count[board_url] == _MAX_BOARD_FETCH_ATTEMPTS


def test_workable_fetch_detail_board_always_fails_bounded_under_concurrency() -> None:
    """Same bound as above, but siblings fire concurrently (mirrors real drain concurrency) --
    the per-slug lock must still serialize attempts so the bound holds under a race, not just
    sequentially."""
    slug = "deadboard-concurrent"
    board_url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    n = 10
    fetcher = _FakeFetcher(
        board_payloads={board_url: _board_payload(_job("CODE00", "<p>Unreachable.</p>"))},
        board_raises=frozenset({board_url}),
    )
    refs = [
        _ref(apply_url=f"https://apply.workable.com/{slug}/j/CODE{i:02d}", id_=str(i))
        for i in range(n)
    ]
    results: list[str | None] = [None] * n

    async def _worker(i: int) -> None:
        results[i] = await WorkableProvider().fetch_detail(refs[i], fetcher)

    async def _run() -> None:
        async with anyio.create_task_group() as tg:
            for i in range(n):
                tg.start_soon(_worker, i)

    anyio.run(_run)

    assert results == [None] * n
    assert fetcher.board_fetch_count[board_url] <= _MAX_BOARD_FETCH_ATTEMPTS
