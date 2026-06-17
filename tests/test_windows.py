"""Tests for cue-anchored context windows."""

from __future__ import annotations

from ergon_tracker.extract.windows import cue_windows


def test_short_text_passes_through() -> None:
    assert cue_windows("Berlin, Germany") == "Berlin, Germany"
    assert cue_windows(None) is None
    assert cue_windows("") == ""


def test_window_captures_deep_yoe_signal() -> None:
    text = (
        "About us. "
        + ("filler " * 400)
        + "You need 5+ years of experience here. "
        + ("more " * 400)
    )
    out = cue_windows(text)
    assert out is not None
    assert "5+ years of experience" in out
    assert len(out) <= 2000  # bounded


def test_window_captures_salary_signal() -> None:
    text = ("x " * 600) + "The base salary range is $120,000 - $160,000 per year." + ("y " * 600)
    out = cue_windows(text)
    assert out is not None
    assert "$120,000" in out and "$160,000" in out


def test_no_cue_falls_back_to_head() -> None:
    text = "lorem ipsum " * 200  # no money/years/experience cue
    out = cue_windows(text, radius=100)
    assert out is not None
    assert out == text[:100]
