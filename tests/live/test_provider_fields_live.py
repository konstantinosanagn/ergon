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
