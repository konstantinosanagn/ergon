"""AsyncFetcher.conditional_get: the cross-build crawl efficiency primitive (304 handling)."""

from __future__ import annotations

import anyio
import httpx

from ergon_tracker.http import AsyncFetcher

_ETAG = 'W/"abc123"'


def _fetcher(handler) -> AsyncFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AsyncFetcher(client=client)


def test_conditional_get_returns_304_with_no_body() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        # server says "unchanged" when the caller presents the matching validator
        if req.headers.get("If-None-Match") == _ETAG:
            return httpx.Response(304, headers={"ETag": _ETAG})
        return httpx.Response(200, content=b"full payload", headers={"ETag": _ETAG})

    async def main() -> None:
        async with _fetcher(handler) as f:
            res = await f.conditional_get("https://x.test/jobs", etag=_ETAG)
            assert res.not_modified is True
            assert res.status_code == 304
            assert res.body is None  # nothing re-downloaded

    anyio.run(main)


def test_conditional_get_returns_body_and_new_validator_on_200() -> None:
    new_etag = 'W/"def456"'

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"jobs": []}', headers={"ETag": new_etag})

    async def main() -> None:
        async with _fetcher(handler) as f:
            res = await f.conditional_get("https://x.test/jobs", etag='W/"stale"')
            assert res.not_modified is False
            assert res.status_code == 200
            assert res.body == b'{"jobs": []}'
            assert res.etag == new_etag  # new validator to persist

    anyio.run(main)


def test_conditional_get_sends_if_modified_since() -> None:
    seen: dict[str, str | None] = {}
    lm = "Thu, 18 Jun 2026 18:13:19 GMT"

    def handler(req: httpx.Request) -> httpx.Response:
        seen["ims"] = req.headers.get("If-Modified-Since")
        return httpx.Response(304, headers={"Last-Modified": lm})

    async def main() -> None:
        async with _fetcher(handler) as f:
            res = await f.conditional_get("https://x.test/board", last_modified=lm)
            assert seen["ims"] == lm
            assert res.not_modified is True
            assert res.last_modified == lm

    anyio.run(main)


def test_conditional_get_without_validators_does_full_get() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert "If-None-Match" not in req.headers
        assert "If-Modified-Since" not in req.headers
        return httpx.Response(200, content=b"data", headers={"ETag": _ETAG})

    async def main() -> None:
        async with _fetcher(handler) as f:
            res = await f.conditional_get("https://x.test/jobs")
            assert res.not_modified is False
            assert res.body == b"data"
            assert res.etag == _ETAG

    anyio.run(main)
