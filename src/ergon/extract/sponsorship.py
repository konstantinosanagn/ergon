"""Posting-level visa-sponsorship policy, from the job description text (deterministic regex).

Distinct from ``extract/visa.py`` (which says whether the *employer* has historically sponsored
H-1B via DoL data). This reads what the *posting itself* states about sponsorship:

* "visa sponsorship available", "we will sponsor", "sponsorship welcome"      -> True
* "must not require sponsorship now or in the future", "we do not sponsor",
  "must be authorized to work without sponsorship", "not eligible for sponsorship" -> False
* no mention                                                                  -> None (unknown)

Tri-state on purpose: most postings say nothing, so unknown is common and must stay unknown
(never guessed). Negative statements are checked first, because phrasings like "unable to offer
sponsorship" contain the positive substring "offer sponsorship" but clearly mean *no*.
"""

from __future__ import annotations

import re

__all__ = ["detect_sponsorship"]

# Only classify postings that actually mention sponsorship; everything else stays unknown.
_CUE = re.compile(r"sponsor", re.I)

# Negative: the posting states sponsorship is NOT available. Evaluated before positives.
_NEG = re.compile(
    r"(?:"
    r"not?\s+(?:able|eligible|in\s+a\s+position)\s+to\s+(?:provide|offer|sponsor)"
    r"|unable\s+to\s+(?:provide|offer|sponsor)\w*[^.]{0,30}sponsor"
    r"|(?:do(?:es)?\s+not|don'?t|doesn'?t|cannot|can'?t|will\s+not|won'?t|are\s+not|is\s+not)"
    r"\s+(?:be\s+)?(?:able\s+to\s+)?(?:provide|offer|sponsor)\w*[^.]{0,30}sponsor"
    # direct negation of the verb "sponsor" ("will not sponsor", "cannot sponsor", ...)
    r"|(?:do(?:es)?\s+not|don'?t|doesn'?t|cannot|can'?t|will\s+not|won'?t|are\s+not|is\s+not"
    r"|unable\s+to|never)\s+(?:\w+\s+){0,3}sponsor\b"
    r"|no\s+(?:\w+\s+){0,2}sponsorship"  # "no sponsorship", "no visa/immigration/work sponsorship"
    r"|without\s+(?:requiring\s+|the\s+need\s+for\s+)?(?:visa\s+|work\s+|employer\s+|employment\s+)?sponsorship"
    r"|(?:must|should|can|will)\s+not\s+require\s+(?:visa\s+|work\s+)?sponsorship"
    r"|not\s+require\s+(?:visa\s+|work\s+)?sponsorship\s+(?:now|currently|at\s+this\s+time)"
    r"|(?:not|isn'?t|aren'?t)\s+(?:eligible\s+for|open\s+to|able\s+to\s+offer)\s+(?:visa\s+)?sponsorship"
    r"|sponsorship\s+(?:is|are|will)\s*(?:not|n'?t)\s+(?:be\s+)?(?:available|provided|offered)"
    # "Relocation and sponsorship are not available", "not able to support visa sponsorship"
    r"|sponsorship\s+(?:\w+\s+){0,2}(?:is|are)\s+not\s+(?:available|provided|offered)"
    r"|not?\s+(?:able|eligible)\s+to\s+support\s+(?:\w+\s+){0,2}sponsor"
    r"|(?:authoriz|authoris)\w+\s+to\s+work[^.]{0,40}without[^.]{0,25}sponsor"
    r")",
    re.I,
)

# Positive: the posting states sponsorship IS available.
_POS = re.compile(
    r"(?:"
    r"(?:visa\s+|work\s+|h-?1b\s+|employment\s+|green\s*card\s+)?sponsorship\s+(?:is\s+|are\s+)?"
    r"(?:available|offered|provided|welcome|possible|considered|supported|an\s+option)"
    r"|(?:will|can|able\s+to|happy\s+to|open\s+to|willing\s+to|glad\s+to)\s+"
    r"(?:provide\s+|offer\s+)?(?:visa\s+|work\s+|h-?1b\s+)?sponsor\w*"
    r"|(?:can\s+)?help(?:\s+you)?\s+(?:to\s+)?sponsor"  # "we can help sponsor your visa"
    r"|we\s+(?:will\s+|can\s+|do\s+)?sponsor\b"
    r"|(?:offer|provide)\s+(?:visa\s+|work\s+|h-?1b\s+)?sponsorship"
    # "Visa sponsorship for qualified candidates", "provide immigration and sponsorship support"
    r"|(?:visa\s+|work\s+|h-?1b\s+)?sponsorship\s+for\s+(?:qualified|highly|eligible|the\s+right"
    r"|international|exceptional|select|candidates?|applicants?)"
    r"|sponsorship\s+support\b"
    r"|(?:h-?1b|visa)\s+sponsorship\s+(?:is\s+)?available"
    r"|sponsorship\s*[:\-]\s*yes"
    r"|(?:candidates?|applicants?)\s+(?:requiring|needing|who\s+require)\s+sponsorship"
    r"[^.]{0,50}(?:welcome|encouraged|considered|ok|fine)"
    r"|open\s+to\s+(?:candidates?\s+requiring\s+)?sponsorship"
    r")",
    re.I,
)


def detect_sponsorship(text: str | None) -> bool | None:
    """Classify a posting's stated sponsorship policy: True / False / None (unknown)."""
    if not text or not _CUE.search(text):
        return None
    if _NEG.search(text):
        return False
    if _POS.search(text):
        return True
    return None
