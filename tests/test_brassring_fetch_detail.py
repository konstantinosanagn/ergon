"""Tier-3 detail fetcher: BrassRingProvider.fetch_detail.

The list JSON has NO description, so this endpoint is brassring's ONLY JD path. ``fetch_detail``
reuses ``fetch()``'s CSRF handshake (bootstrap GET -> ``RFT`` token + session cookies), POSTs the
``JobDetails`` AJAX record, resolves the JD from ``JobDetailQuestions`` (by the tenant's
``Summary`` field code via ``VerityZone``, else a ``QuestionName`` fallback), and captures the
location. Obeys the base contract (``None`` == GONE: real 404/410 or a null ``Jobdetails``; raise
== indeterminate).

Offline only -- a fake fetcher stands in for AsyncFetcher (``get_text`` for the bootstrap,
``request`` for the POST); no live network."""

from __future__ import annotations

import anyio
import pytest

from ergon_tracker.index.detail import DetailRef
from ergon_tracker.models import DetailFetch
from ergon_tracker.providers.brassring import BrassRingProvider

_BOOTSTRAP_HTML = (
    '<html><body><input name="__RequestVerificationToken" value="TESTRFT">'
    '<input id="CookieValue" value="^enc"></body></html>'
)
_BOOTSTRAP_NO_TOKEN = "<html><body><p>no token here</p></body></html>"


class _FakeResponse:
    def __init__(self, status_code: int, *, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeFetcher:
    """Serves the bootstrap HTML on ``get_text`` and a canned POST response on ``request``."""

    def __init__(
        self,
        *,
        bootstrap_html: str = _BOOTSTRAP_HTML,
        post_response: _FakeResponse | None = None,
        post_raises: BaseException | None = None,
    ) -> None:
        self._bootstrap_html = bootstrap_html
        self._post_response = post_response
        self._post_raises = post_raises
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    async def get_text(self, url: str, **kw: object) -> str:
        self.get_calls.append(url)
        return self._bootstrap_html

    async def request(self, method: str, url: str, **kw: object) -> _FakeResponse:
        self.post_calls.append((url, dict(kw.get("json") or {})))
        if self._post_raises is not None:
            raise self._post_raises
        assert self._post_response is not None, "test must set a post_response"
        return self._post_response


def _service(questions: list[dict], *, summary: str | None = "formtext3") -> dict:
    fields = {"Summary": summary} if summary is not None else {}
    return {
        "ServiceResponse": {
            "JobFieldsToDisplay": fields,
            "Jobdetails": {"JobDetailQuestions": questions},
        }
    }


def _ref(
    *,
    id: str = "3343391",
    token: str | None = "sjobs.brassring.com|25416|5429",
    apply_url: str | None = None,
    listing_url: str | None = None,
) -> DetailRef:
    return DetailRef(
        id=id,
        source="brassring",
        token=token,
        apply_url=apply_url,
        listing_url=listing_url,
        content_sig="s",
    )


def test_brassring_alive_resolves_jd_by_verityzone_and_location() -> None:
    questions = [
        {"QuestionName": "Job Description", "VerityZone": "formtext3",
         "AnswerValue": "<p>Generate new business opportunities.</p>"},
        {"QuestionName": "Location City/Town/District", "VerityZone": "formtext8",
         "AnswerValue": "Decatur"},
        {"QuestionName": "Location Country", "VerityZone": "formtext9", "AnswerValue": "USA"},
    ]
    fetcher = _FakeFetcher(post_response=_FakeResponse(200, payload=_service(questions)))
    res = anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))
    assert isinstance(res, DetailFetch)
    assert res.text == "<p>Generate new business opportunities.</p>"
    assert res.locations is not None
    loc = res.locations[0]
    assert loc.raw == "Decatur, USA" and loc.city == "Decatur" and loc.country == "USA"
    # Handshake then POST, in order.
    assert fetcher.get_calls == [
        "https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=25416&siteid=5429"
    ]
    url, body = fetcher.post_calls[0]
    assert url == "https://sjobs.brassring.com/TgNewUI/Search/Ajax/JobDetails"
    assert body == {
        "partnerId": "25416", "siteId": "5429", "jobid": "3343391",
        "configMode": 1, "jobSiteId": "5429",
    }


def test_brassring_jd_falls_back_to_questionname_when_summary_unset() -> None:
    # No Summary field code -> resolve by the "Job Description" QuestionName label.
    questions = [{"QuestionName": "Job Description", "VerityZone": "zzz", "AnswerValue": "Body."}]
    fetcher = _FakeFetcher(
        post_response=_FakeResponse(200, payload=_service(questions, summary=None))
    )
    res = anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))
    assert res == "Body."  # no location questions -> bare str


def test_brassring_jd_summary_questionname_fallback() -> None:
    # Tenant whose JD question is labelled "Summary" (Fairfax-style), field code jobdescription.
    questions = [
        {"QuestionName": "Summary", "VerityZone": "jobdescription", "AnswerValue": "Duties here."}
    ]
    fetcher = _FakeFetcher(
        post_response=_FakeResponse(200, payload=_service(questions, summary="jobdescription"))
    )
    res = anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))
    assert res == "Duties here."


def test_brassring_404_returns_none() -> None:
    fetcher = _FakeFetcher(post_response=_FakeResponse(404))
    res = anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))
    assert res is None


def test_brassring_410_returns_none() -> None:
    fetcher = _FakeFetcher(post_response=_FakeResponse(410))
    res = anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))
    assert res is None


def test_brassring_null_jobdetails_returns_none() -> None:
    payload = {"ServiceResponse": {"Jobdetails": None}}
    fetcher = _FakeFetcher(post_response=_FakeResponse(200, payload=payload))
    res = anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))
    assert res is None


def test_brassring_missing_csrf_token_raises() -> None:
    fetcher = _FakeFetcher(bootstrap_html=_BOOTSTRAP_NO_TOKEN, post_response=_FakeResponse(200))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))
    assert fetcher.post_calls == []  # never POSTs without a token


def test_brassring_unexpected_status_raises() -> None:
    fetcher = _FakeFetcher(post_response=_FakeResponse(500))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))


def test_brassring_no_resolvable_jd_raises() -> None:
    # Live posting whose JD field can't be resolved -> indeterminate, never None (single-miss source).
    questions = [{"QuestionName": "Job Title", "VerityZone": "jobtitle", "AnswerValue": "Engineer"}]
    fetcher = _FakeFetcher(post_response=_FakeResponse(200, payload=_service(questions)))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))


def test_brassring_non_dict_payload_raises() -> None:
    fetcher = _FakeFetcher(post_response=_FakeResponse(200, payload=["nope"]))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))


def test_brassring_transient_post_error_propagates() -> None:
    fetcher = _FakeFetcher(post_raises=RuntimeError("boom (5xx after retries)"))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BrassRingProvider().fetch_detail(_ref(), fetcher))


def test_brassring_params_fallback_from_apply_url() -> None:
    # No token -> host/pid/sid/jobid parsed from the posting Link query params.
    questions = [{"QuestionName": "Job Description", "VerityZone": "formtext3", "AnswerValue": "JD"}]
    fetcher = _FakeFetcher(post_response=_FakeResponse(200, payload=_service(questions)))
    ref = _ref(
        id="",
        token=None,
        apply_url=(
            "https://krb-sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad"
            "?partnerid=99&siteid=88&PageType=JobDetails&jobid=555"
        ),
    )
    res = anyio.run(lambda: BrassRingProvider().fetch_detail(ref, fetcher))
    assert res == "JD"
    assert fetcher.get_calls == [
        "https://krb-sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=99&siteid=88"
    ]
    _, body = fetcher.post_calls[0]
    assert body["partnerId"] == "99" and body["siteId"] == "88" and body["jobid"] == "555"


def test_brassring_unbuildable_ref_raises() -> None:
    ref = _ref(id="", token=None, apply_url=None, listing_url=None)
    fetcher = _FakeFetcher(post_response=_FakeResponse(200))
    with pytest.raises(RuntimeError):
        anyio.run(lambda: BrassRingProvider().fetch_detail(ref, fetcher))
    assert fetcher.get_calls == []  # bails before any network
