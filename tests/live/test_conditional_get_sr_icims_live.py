"""Live re-verification for the Phase-1 delta-crawl conditional-GET investigation.

Both smartrecruiters and icims (new "Career Sites"/Jibe gen) DO honor conditional GET on their
listing endpoint -- a stored ETag reliably produces a real 304. But both paginate (server-side
page cap of 100), and each page carries its OWN ETag independent of the others. So a 304 on the
page-1 URL (the one URL ``conditional_url`` could plausibly expose) proves only "page 1 is
unchanged" -- NOT "the whole board is unchanged" for any board with more than one page.

Per ``providers/base.py``'s ``conditional_url`` contract ("validates this board's WHOLE
response"), that disqualifies a page-1 override once a board exceeds the page size, and board
size can grow past the page size over time -- so neither provider defines ``conditional_url``
(see ``tests/test_provider_conditional_url.py``). This test hits real multi-page boards and
proves both halves of that decision: the 304 is real, and it does not cover the whole board.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

with open(Path(__file__).resolve().parents[2] / "src/ergon_tracker/registry/data/seed.json") as _f:
    _SEED = json.load(_f)["companies"]
_H = {"User-Agent": "Mozilla/5.0 (conditional-GET probe)"}
_SR_PAGE_LIMIT = 100
_ICIMS_PAGE_LIMIT = 100


def _tokens(ats: str, n: int) -> list[str]:
    return [
        e["token"]
        for e in _SEED.values()
        if isinstance(e, dict) and e.get("ats") == ats and e.get("token")
    ][:n]


def _find_multi_page_sr_board() -> tuple[str, int] | None:
    """Return ``(token, totalFound)`` for the first sampled SR board spanning > one page."""
    for token in _tokens("smartrecruiters", 60):
        url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
        try:
            r = httpx.get(url, params={"limit": _SR_PAGE_LIMIT, "offset": 0}, headers=_H, timeout=15)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        total = r.json().get("totalFound") or 0
        if total > _SR_PAGE_LIMIT:
            return token, total
    return None


def _find_multi_page_icims_new_board() -> tuple[str, int] | None:
    """Return ``(host, totalCount)`` for the first sampled new-gen icims board with > one page."""
    for token in _tokens("icims", 60):
        host = token.split("|", 1)[0]
        url = f"https://{host}/api/jobs"
        try:
            r = httpx.get(url, params={"page": 1, "limit": _ICIMS_PAGE_LIMIT}, headers=_H, timeout=15)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except ValueError:
            continue  # classic-gen host answers HTML, not JSON -- skip, not our target here
        if not isinstance(data, dict):
            continue
        total = data.get("totalCount")
        if isinstance(total, int) and total > _ICIMS_PAGE_LIMIT:
            return host, total
    return None


@pytest.mark.live
def test_smartrecruiters_page1_conditional_get_304s_but_not_whole_board():
    found = _find_multi_page_sr_board()
    assert found is not None, "no sampled SmartRecruiters board exceeded one page (100 postings)"
    token, total = found
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"

    r1 = httpx.get(url, params={"limit": _SR_PAGE_LIMIT, "offset": 0}, headers=_H, timeout=15)
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag, f"{token}: page-1 response carried no ETag"

    # Conditional GET on the exact same page-1 URL -> a real 304 (the mechanism works).
    r2 = httpx.get(
        url,
        params={"limit": _SR_PAGE_LIMIT, "offset": 0},
        headers={**_H, "If-None-Match": etag},
        timeout=15,
    )
    assert r2.status_code == 304, f"{token}: expected 304, got {r2.status_code}"

    # But page 2's ETag is independent of page 1's -- proving the page-1 304 says nothing
    # about the rest of a >100-posting board (total sampled = {total}).
    r3 = httpx.get(
        url, params={"limit": _SR_PAGE_LIMIT, "offset": _SR_PAGE_LIMIT}, headers=_H, timeout=15
    )
    assert r3.status_code == 200
    assert r3.headers.get("etag") != etag, (
        f"{token}: page-2 ETag matched page-1 ({etag}) -- if this starts holding board-wide, "
        "conditional_url may become safe to wire; re-review before relaxing this assertion"
    )


@pytest.mark.live
def test_icims_new_gen_page1_conditional_get_304s_but_not_whole_board():
    found = _find_multi_page_icims_new_board()
    assert found is not None, "no sampled new-gen icims board exceeded one page (100 postings)"
    host, total = found
    url = f"https://{host}/api/jobs"

    r1 = httpx.get(url, params={"page": 1, "limit": _ICIMS_PAGE_LIMIT}, headers=_H, timeout=15)
    assert r1.status_code == 200
    etag = r1.headers.get("etag")
    assert etag, f"{host}: page-1 response carried no ETag"

    r2 = httpx.get(
        url,
        params={"page": 1, "limit": _ICIMS_PAGE_LIMIT},
        headers={**_H, "If-None-Match": etag},
        timeout=15,
    )
    assert r2.status_code == 304, f"{host}: expected 304, got {r2.status_code}"

    r3 = httpx.get(url, params={"page": 2, "limit": _ICIMS_PAGE_LIMIT}, headers=_H, timeout=15)
    assert r3.status_code == 200
    assert r3.headers.get("etag") != etag, (
        f"{host}: page-2 ETag matched page-1 ({etag}) -- if this starts holding board-wide, "
        "conditional_url may become safe to wire; re-review before relaxing this assertion"
    )


@pytest.mark.live
def test_icims_classic_gen_listing_has_no_conditional_headers():
    # careers-winco.icims.com is the classic-gen board verified live in icims.py's docstring
    # (10 pages of listings, JSON-LD on every detail). The classic listing endpoint is HTML,
    # not the JSON API, and is the only per-board "index" surface classic-gen exposes.
    url = "https://careers-winco.icims.com/jobs/search?in_iframe=1&pr=0"
    r = httpx.get(url, headers=_H, timeout=15)
    assert r.status_code == 200
    assert r.headers.get("etag") is None
    assert r.headers.get("last-modified") is None
    cache_control = (r.headers.get("cache-control") or "").lower()
    assert "no-store" in cache_control or "no-cache" in cache_control
