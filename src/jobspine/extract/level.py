"""Job level / seniority extraction (rules baseline)."""

from __future__ import annotations

import re

from ..models import JobLevel
from .base import ExtractInput, register_extractor

__all__ = ["infer_level", "LevelExtractor"]

# Ordered: first hit wins, so list the strongest seniority signal first.
_LEVEL_PATTERNS: list[tuple[JobLevel, re.Pattern[str]]] = [
    (JobLevel.INTERN, re.compile(r"\b(intern|internship|co-?op|apprentice|werk?student)\b", re.I)),
    (
        JobLevel.EXECUTIVE,
        re.compile(r"\b(chief|c[etfoi]o|cxo|cmo|cpo|svp|evp|vp|vice president|head of)\b", re.I),
    ),
    (JobLevel.DIRECTOR, re.compile(r"\bdirector\b", re.I)),
    (JobLevel.MANAGER, re.compile(r"\b(manager|mgr|people lead)\b", re.I)),
    (JobLevel.PRINCIPAL, re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    (JobLevel.STAFF, re.compile(r"\bstaff\b", re.I)),
    (JobLevel.LEAD, re.compile(r"\b(lead|tech lead|team lead)\b", re.I)),
    (JobLevel.SENIOR, re.compile(r"\b(senior|sr\.?|snr)\b", re.I)),
    (JobLevel.JUNIOR, re.compile(r"\b(junior|jr\.?|jnr)\b", re.I)),
    (
        JobLevel.ENTRY,
        re.compile(r"\b(entry[- ]level|new ?grad|graduate|associate|trainee|early career)\b", re.I),
    ),
]


def infer_level(title: str) -> JobLevel:
    """Infer seniority from a job title. Returns UNKNOWN when no signal is present."""
    for level, pattern in _LEVEL_PATTERNS:
        if pattern.search(title or ""):
            return level
    return JobLevel.UNKNOWN


class LevelExtractor:
    name = "level"

    def extract(self, inp: ExtractInput) -> JobLevel:
        return infer_level(inp.title)


register_extractor(LevelExtractor())
