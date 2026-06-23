"""Unit tests for the browser_queue triage classifier (pure verdict logic)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from browser_queue_triage import classify

_JOB = "We are hiring! Open positions and careers. Apply now." + "x" * 2100


def test_parking_and_off_domain_rejected_by_host():
    # hugedomains parking page whose URL carries ?d=metaoptics.com — a substring check would falsely pass
    assert (
        classify(200, "buy this domain " + _JOB, "www.hugedomains.com", "metaoptics.com")["verdict"]
        == "dead-parked"
    )
    # acquirer redirect: final host's registrable domain != company's
    assert classify(200, _JOB, "insightsoftware.com", "magnitude.com")["verdict"] == "off-domain"


def test_ats_iframe_beats_api_and_static():
    body = _JOB + " <iframe src='https://acme.comeet.co/jobs'></iframe> /api/jobs"
    v = classify(200, body, "acme.com", "acme.com")
    assert v["verdict"] == "ats-iframe" and "comeet" in v["ats"]  # missed-ATS routed to ladder


def test_api_spa_vs_static():
    assert (
        classify(200, _JOB + " fetch('/wp-json/wp/v2/jobs')", "x.com", "x.com")["verdict"]
        == "api-spa"
    )
    assert classify(200, _JOB, "x.com", "x.com")["verdict"] == "static-html"


def test_inconclusive_returns_none():
    assert classify(404, "", "x.com", "x.com") is None  # non-200 -> try next path
    assert classify(200, "thin", "x.com", "x.com") is None  # too short
    assert classify(200, "x" * 3000, "x.com", "x.com") is None  # no job signal
