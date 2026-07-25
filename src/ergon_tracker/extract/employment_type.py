"""Employment-type extractor (deterministic gazetteer + regex, no ML).

Derives ``full_time / part_time / contract / internship / temporary`` from the title first
(highest precision) and, failing that, from a small set of *position-framed* description phrases.
Precision-first, like every extractor here:

* An explicit ``Employment Type: <value>`` / ``Job Type: <value>`` label (many ATSes emit one)
  is the strongest signal and is tried first.
* The title is tried next on whole-word forms — ``\\bintern\\b`` cannot fire inside "internal"
  and bare ``contract`` in a title reliably means the engagement, not a job duty.
* Description matching is deliberately narrow: it requires the type word to be *framed* as the
  engagement ("full-time position", "6-month contract", "paid internship"), never a bare mention
  — so a benefits sentence ("full-time employees are eligible …") in a contract JD can't mislabel
  it, and "contract negotiation" / "contract management" (job DUTIES) never fire.
* ``full_time`` is checked LAST: it is the common default and the most likely to appear as
  boilerplate, so any more-specific match (internship/contract/temporary/part_time) wins.

Returns ``EmploymentType.UNKNOWN`` when nothing matches — enrichment then leaves the field
untouched, exactly as the other tri-state extractors do.
"""

from __future__ import annotations

import re

from ..models import EmploymentType
from .base import ExtractInput, register_extractor

__all__ = ["EmploymentTypeExtractor"]

# --- explicit ATS label ("Employment Type: Contract", "Job Type - Part-time") ----------------
# The value is captured then normalized against the same body patterns below, so every surface
# form ("Full Time", "full-time", "Intern") maps once.
_LABEL = re.compile(
    r"\b(?:employment|position|job|engagement|work|contract)\s+type\s*[:\-–]\s*"
    r"([A-Za-z][A-Za-z /\-]{2,20})",
    re.I,
)

# bare "contract" in a title is a domain false-friend ("Smart Contract" = blockchain; "Permanent
# Contract" = a PERMANENT role in EU parlance, not a contractor engagement), so it fires only when
# unambiguously engagement-framed: an explicit contractor form, a parenthetical/dash "(Contract)",
# a fixed-term/N-month contract, or "contract role/position/basis/to-hire". Precision over recall.
_CONTRACT_TITLE = re.compile(
    r"\b(?:contractor|freelance|1099|c2c|corp[-\s]?to[-\s]?corp)\b"
    r"|\bfixed[-\s]?term\s+contract\b|\b\d+[-\s]?months?\s+contract\b"
    r"|(?<!smart\s)(?<!permanent\s)\bcontract\b"
    r"(?=\s*[):\-–]|\s+(?:role|position|basis|assignment|opportunity)\b|\s+to[-\s]?hire\b)",
    re.I,
)

# --- explicit-label value normalization (bare forms) ------------------------------------------
# Applied ONLY to a value captured after "Employment Type:" etc., where the word is already
# unambiguous — so bare "Contract" counts (no framing needed) and "Permanent"/"Regular" -> full.
_LABEL_VALUES: tuple[tuple[re.Pattern[str], EmploymentType], ...] = (
    (re.compile(r"\b(?:intern|internship|co[-\s]?op)\b", re.I), EmploymentType.INTERNSHIP),
    (
        re.compile(r"\b(?:contract(?:or)?|freelance|1099|c2c)\b", re.I),
        EmploymentType.CONTRACT,
    ),
    (re.compile(r"\bpart[-\s]?time\b", re.I), EmploymentType.PART_TIME),
    (
        re.compile(r"\b(?:temporary|temp|seasonal|fixed[-\s]?term)\b", re.I),
        EmploymentType.TEMPORARY,
    ),
    (
        re.compile(r"\b(?:full[-\s]?time|permanent|regular)\b", re.I),
        EmploymentType.FULL_TIME,
    ),
)

# --- title patterns (bare whole-word forms — high precision from the title) -------------------
_TITLE: tuple[tuple[re.Pattern[str], EmploymentType], ...] = (
    (re.compile(r"\b(?:intern|internship|co[-\s]?op)\b", re.I), EmploymentType.INTERNSHIP),
    (_CONTRACT_TITLE, EmploymentType.CONTRACT),
    (re.compile(r"\bpart[-\s]?time\b", re.I), EmploymentType.PART_TIME),
    (
        re.compile(r"\b(?:temporary|temp|seasonal|fixed[-\s]?term)\b", re.I),
        EmploymentType.TEMPORARY,
    ),
    (re.compile(r"\bfull[-\s]?time\b", re.I), EmploymentType.FULL_TIME),
)

# --- description patterns (require the type word FRAMED as the engagement) --------------------
# Ordered specific -> generic; full_time last so boilerplate never outranks a specific match.
_ROLE = r"(?:position|role|opportunity|assignment|employment|job|hire|basis|work|engagement)"
_BODY: tuple[tuple[re.Pattern[str], EmploymentType], ...] = (
    (
        re.compile(
            rf"\b(?:paid\s+|summer\s+|winter\s+|fall\s+|spring\s+)?internship\b"
            rf"|\b(?:intern|co[-\s]?op)\s+(?:{_ROLE}|program)\b",
            re.I,
        ),
        EmploymentType.INTERNSHIP,
    ),
    (
        re.compile(
            rf"\bcontract\s+(?:{_ROLE}|to[-\s]?hire)\b|\bcontract[-\s]?to[-\s]?hire\b"
            rf"|\bon\s+a\s+contract\b|\b\d+[-\s]?month\s+contract\b|\bfreelance\s+{_ROLE}\b",
            re.I,
        ),
        EmploymentType.CONTRACT,
    ),
    (
        re.compile(
            rf"\b(?:temporary|fixed[-\s]?term)\s+{_ROLE}\b|\bseasonal\s+{_ROLE}\b"
            rf"|\btemporary\s+basis\b|\bfixed[-\s]?term\s+contract\b",
            re.I,
        ),
        EmploymentType.TEMPORARY,
    ),
    (re.compile(rf"\bpart[-\s]?time\s+{_ROLE}\b|\bpart[-\s]?time\b", re.I), EmploymentType.PART_TIME),
    (
        re.compile(rf"\bfull[-\s]?time\s+{_ROLE}\b|\bthis\s+is\s+a\s+full[-\s]?time\b", re.I),
        EmploymentType.FULL_TIME,
    ),
)


def _from_text(text: str, patterns: tuple[tuple[re.Pattern[str], EmploymentType], ...]) -> EmploymentType:
    for pattern, etype in patterns:
        if pattern.search(text):
            return etype
    return EmploymentType.UNKNOWN


class EmploymentTypeExtractor:
    """Extract an :class:`EmploymentType` from a posting's title + description."""

    name = "employment_type"

    def extract(self, inp: ExtractInput) -> EmploymentType:
        # 1) explicit ATS label anywhere in the text (strongest signal).
        if inp.description_text:
            m = _LABEL.search(inp.description_text)
            if m:
                # The captured value is a bare, already-unambiguous type word ("Contract",
                # "Full Time", "Permanent") — normalize it with the bare-form label map.
                labeled = _from_text(m.group(1), _LABEL_VALUES)
                if labeled is not EmploymentType.UNKNOWN:
                    return labeled
        # 2) title (bare whole-word forms — high precision).
        titled = _from_text(inp.title, _TITLE)
        if titled is not EmploymentType.UNKNOWN:
            return titled
        # 3) framed phrases in the description body.
        if inp.description_text:
            return _from_text(inp.description_text, _BODY)
        return EmploymentType.UNKNOWN


register_extractor(EmploymentTypeExtractor())
