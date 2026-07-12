"""Populated-fill gates for structured ATS fields we plan to map into JobPosting.

These hit real ATS APIs to prove a field is actually POPULATED (not merely present)
before we bother mapping it in a provider's ``normalize()``. Fill rate means populated,
never present — see the SmartRecruiters ``experienceLevel`` gate below.
"""

import json
from pathlib import Path

import httpx
import pytest

with open(
    Path(__file__).resolve().parents[2] / "src/ergon_tracker/registry/data/seed.json"
) as _f:
    _SEED = json.load(_f)["companies"]
_H = {"User-Agent": "Mozilla/5.0 (populated-fill gate)"}


def _tokens(ats, n):
    return [
        e["token"]
        for e in _SEED.values()
        if isinstance(e, dict) and e.get("ats") == ats and e.get("token")
    ][:n]


@pytest.mark.live
def test_smartrecruiters_experiencelevel_populated():
    tot = filled = 0
    for t in _tokens("smartrecruiters", 12):
        try:
            d = httpx.get(
                f"https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=50",
                headers=_H,
                timeout=15,
            ).json()
        except Exception:
            continue
        for p in d.get("content", []):
            tot += 1
            lvl = (p.get("experienceLevel") or {}).get("label")
            if lvl:
                filled += 1
    assert tot >= 50, f"too few sampled ({tot})"
    assert filled / tot >= 0.80, f"experienceLevel populated only {filled}/{tot}"


@pytest.mark.live
def test_jazzhr_experience_populated():
    tot = filled = 0
    for t in _tokens("jazzhr", 10):
        try:
            r = httpx.get(f"https://app.jazz.co/feeds/export/jobs/{t}", headers=_H, timeout=15)
        except Exception:
            continue
        import re

        for m in re.findall(r"<experience>(.*?)</experience>", r.text, re.S):
            tot += 1
            if m.replace("<![CDATA[", "").replace("]]>", "").strip():
                filled += 1
    assert tot >= 30 and filled / tot >= 0.80, f"jazzhr experience {filled}/{tot}"


@pytest.mark.live
def test_workable_experience_populated():
    tot = filled = 0
    for t in _tokens("workable", 24):
        try:
            d = httpx.get(f"https://apply.workable.com/api/v1/widget/accounts/{t}",
                          headers=_H, timeout=15).json()
        except Exception:
            continue
        for p in d.get("jobs", []):
            tot += 1
            if p.get("experience"):
                filled += 1
    assert tot >= 200, f"too few sampled ({tot})"
    assert filled / tot >= 0.55, f"workable experience {filled}/{tot}"


@pytest.mark.live
def test_join_salary_amount_populated():
    # Deviation from the brief's 8-token sample: join boards carry very few
    # salary-bearing postings each (~0.5/token measured), so 8 tokens gave tot=5 (<30) on
    # two independent runs against real 200 responses -- a genuine small-sample shortfall,
    # not a network flake. 80 tokens reliably clears tot>=30 (measured tot=40, 100% filled)
    # while leaving the >=0.30 ratio bar untouched.
    import re

    tot = filled = 0
    for t in _tokens("join", 80):
        try:
            r = httpx.get(f"https://join.com/companies/{t}", headers=_H, timeout=15)
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            d = json.loads(m.group(1))
        except Exception:
            continue
        stack = [d]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                if o.get("title") and "salaryAmountFrom" in o:
                    tot += 1
                    if o.get("salaryAmountFrom"):
                        filled += 1
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    assert tot >= 30 and filled / tot >= 0.30, f"join salaryAmountFrom {filled}/{tot}"


@pytest.mark.live
def test_breezy_salary_populated():
    # Deviation from the brief's 12-token sample: widened to 18 tokens so tot>=100
    # is reliably cleared even if 1-2 boards are dead/empty, per the flaky-sample
    # lesson from the join gate above. Ratio bar (>=0.30) unchanged from the brief.
    tot = filled = 0
    for t in _tokens("breezy", 18):
        try:
            arr = httpx.get(f"https://{t}.breezy.hr/json", headers=_H, timeout=15).json()
        except Exception:
            continue
        for p in arr:
            tot += 1
            s = p.get("salary")
            if isinstance(s, str) and s.strip():
                filled += 1
    assert tot >= 100 and filled / tot >= 0.30, f"breezy salary {filled}/{tot}"


@pytest.mark.live
def test_personio_seniority_populated():
    # Personio serves an XML feed; parse <seniority>/<yearsOfExperience> directly (like the jazzhr
    # gate) rather than driving the provider's async fetch. 20 tokens so 1-2 dead boards can't flip
    # the ratio. Controller-measured: seniority 100% (376/376), years 82% across 19 live boards.
    import re

    tot = filled = 0
    for t in _tokens("personio", 20):
        text = None
        for url in (f"https://{t}.jobs.personio.de/xml", f"https://{t}.jobs.personio.com/xml"):
            try:
                r = httpx.get(url, headers=_H, timeout=12)
            except Exception:
                continue
            if r.status_code == 200 and "<position>" in r.text:
                text = r.text
                break
        if not text:
            continue
        for pos in re.findall(r"<position>(.*?)</position>", text, re.S):
            tot += 1
            m = re.search(r"<seniority>(.*?)</seniority>", pos, re.S)
            val = (m.group(1).replace("<![CDATA[", "").replace("]]>", "").strip() if m else "")
            if val:
                filled += 1
    assert tot >= 30, f"too few sampled ({tot})"
    assert filled / tot >= 0.70, f"personio seniority {filled}/{tot}"
