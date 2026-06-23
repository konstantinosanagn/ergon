"""Tests for apply-assist: the skills gazetteer + the assess_fit MCP tool (deterministic gap analysis)."""

from __future__ import annotations

from ergon_tracker import mcp_server
from ergon_tracker.extract.skills import extract_skills


def test_skills_special_chars_and_boundaries():
    assert extract_skills("Strong C++ and C# with .NET") == {"c++", "c#", ".net"}
    assert "machine learning" not in extract_skills("Build HTML pages")  # 'ml' not inside 'html'
    assert "machine learning" in extract_skills("5 years of ML")
    assert extract_skills("Deploy on k8s with CI/CD") == {"kubernetes", "ci/cd"}
    assert "golang" in extract_skills("using Golang") and "golang" not in extract_skills(
        "go to the store"
    )


def test_skills_phrases_and_empty():
    s = extract_skills("Ruby on Rails, Google Cloud, data engineering, React.js")
    assert {"rails", "gcp", "data engineering", "react"} <= s
    assert extract_skills("") == set() and extract_skills(None) == set()


def test_assess_fit_gap_analysis():
    resume = "Senior engineer, 7 years of experience in Python, AWS, PostgreSQL and Docker."
    jd = "Requires 5+ years of experience with Python, Kubernetes and AWS. Docker a plus."
    res = mcp_server.assess_fit(resume=resume, job_description=jd, job_title="Backend Engineer")
    assert {"python", "aws"} <= set(res["matched_skills"])
    assert "kubernetes" in res["missing_skills"]  # in JD, not résumé -> a gap
    assert res["required_years"] == 5 and res["your_years"] == 7 and res["meets_years"] is True
    assert res["skill_coverage"] is not None
    assert any("kubernetes" in g.lower() for g in res["gaps_to_address"])
    assert any("python" in t.lower() for t in res["talking_points"])


def test_assess_fit_years_shortfall_adds_gap():
    res = mcp_server.assess_fit(
        resume="2 years of experience in python",
        job_description="requires 6+ years of experience in python",
    )
    assert res["required_years"] == 6 and res["your_years"] == 2 and res["meets_years"] is False
    assert any("years" in g for g in res["gaps_to_address"])


def test_assess_fit_requires_both_inputs():
    assert "note" in mcp_server.assess_fit(resume="", job_description="x")
    assert "note" in mcp_server.assess_fit(resume="x", job_description="   ")
