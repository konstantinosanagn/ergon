"""Cue-anchored context windows.

Long job descriptions (median ~4.7k chars) bury the signal for compensation and
years-of-experience far from the start. Rather than truncate from the head (which drops ~97%
of YoE statements) or keep the whole document (bloat), we keep small windows around the
regex cues. This is used to build compact eval corpora and can serve as evidence snippets.
"""

from __future__ import annotations

import re

__all__ = ["cue_windows", "CUE_RE"]

# Broad cue superset (intentionally wider than any single extractor's final rules, so we never
# bake a miss into the windowed corpus): money, pay words, and "<n> years"/experience.
CUE_RE = re.compile(
    r"\$|£|€|\bUSD\b|\bEUR\b|\bGBP\b|\bCAD\b|\bAUD\b"
    r"|\bsalary\b|\bcompensation\b|\bpay\b|\bOTE\b|\bbase\b"
    r"|\bexperience\b|\bexperienced\b"
    r"|\b(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)


def cue_windows(text: str | None, *, radius: int = 250, max_total: int = 2000) -> str | None:
    """Return cue-anchored windows of ``text`` joined by ' … ', capped at ``max_total`` chars.

    Falls back to the head of the text when no cue is present; passes through short/empty text.
    """
    if not text:
        return text
    if len(text) <= radius * 2:
        return text
    spans: list[list[int]] = []
    for m in CUE_RE.finditer(text):
        start, end = max(0, m.start() - radius), min(len(text), m.end() + radius)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    if not spans:
        return text[:radius]
    joined = " … ".join(text[s:e] for s, e in spans)
    return joined[:max_total]
