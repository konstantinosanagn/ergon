"""Job level / seniority extraction (deterministic rules baseline).

The classifier is a strictly ordered set of dictionary/regex rules. The strongest
signal must come first, because the first match wins. Key design points:

* Explicit seniority words (senior/staff/principal/lead/junior/intern) beat IC
  "manager" head-nouns and numeric ladder tokens, e.g. "Senior Project Manager"
  resolves to SENIOR rather than MANAGER.
* "Manager" is only a people-management level when it is the *leading* head noun
  ("Manager, Data Engineering") or a recognised team-leadership discipline
  ("Engineering Manager", "District Manager"). A trailing "<IC function> Manager"
  (product/account/marketing/operations/...) is an individual-contributor title
  and is intentionally left to fall through (typically UNKNOWN), so seniority
  qualifiers win for those.
* "Head of X" and "(Assistant) Vice President"/AVP are director-level, while
  EXECUTIVE is reserved for Chief/C?O/President/VP/SVP/EVP.
"""

from __future__ import annotations

import re

from ..models import JobLevel
from .base import ExtractInput, register_extractor

__all__ = ["infer_level", "level_from_years", "level_from_description", "LevelExtractor"]

# Description-level cues — ONLY high-confidence early-career phrases. Job-description prose is
# noisy (the reason title+desc sector classification was ~24% accurate), so we deliberately infer
# only the RELIABLE entry/intern signals here; senior is left to the title + years-of-experience
# (a generic "senior" in a JD — "senior stakeholders", "report to senior leadership" — is not a
# level signal). "graduate" alone is excluded (graduate degree/school); only explicit new-grad
# phrasings match.
_DESC_INTERN = re.compile(
    r"\b(?:internship|summer intern|intern(?:ship)?\s+(?:program|position)|co-?op program)\b", re.I
)
_DESC_ENTRY = re.compile(
    r"\b(?:new[- ]?grad(?:uate)?s?|recent (?:college )?graduates?|new college graduates?"
    r"|entry[- ]level (?:role|position|opportunity|hire)?|early[- ]career"
    r"|university graduate program|campus (?:hire|recruit(?:ing)?)"
    r"|no (?:prior |professional )?experience (?:is )?(?:required|necessary|needed))\b",
    re.I,
)


def level_from_description(text: str | None) -> JobLevel:
    """Coarse level from HIGH-confidence description phrases (entry/intern only); else UNKNOWN.

    Recovers early-career roles whose TITLE is bare ("Software Engineer") but whose JD says
    "new grad" / "entry-level" / "no experience required". Conservative by design — never infers
    mid/senior from prose (unreliable); those come from the title or years-of-experience.
    """
    if not text:
        return JobLevel.UNKNOWN
    if _DESC_INTERN.search(text):
        return JobLevel.INTERN
    if _DESC_ENTRY.search(text):
        return JobLevel.ENTRY
    return JobLevel.UNKNOWN


def level_from_years(min_years: int | None, max_years: int | None) -> JobLevel:
    """Opt-in fallback: map a stated years-of-experience requirement to a coarse level.

    Conservative on purpose — caps at SENIOR (staff/principal need an explicit title signal).
    Uses the lower bound (the requirement floor). Returns UNKNOWN when no years are given.
    """
    years = min_years if min_years is not None else max_years
    if years is None:
        return JobLevel.UNKNOWN
    if years <= 1:
        return JobLevel.ENTRY
    if years <= 4:
        return JobLevel.MID
    return JobLevel.SENIOR


# --- Individual-contributor "Manager" functions -----------------------------
# "<function> Manager/Mgr" is an IC role title, not a people-management level.
# Matched only when the function word sits immediately before manager/mgr.
# Kept broad: these are the functions the gold set treats as IC contributors,
# so a seniority qualifier ("Senior ... Manager") wins and a bare title falls
# through to UNKNOWN rather than MANAGER.
_IC_MANAGER = re.compile(
    r"\b(?:"
    r"product marketing|customer success|technical success|client success|"
    r"product|account|project|program|programme|marketing|community|"
    r"channel|category|brand|growth|partner|partnership|partnerships|sales|"
    r"operations|success|delivery|implementation|launch|market|country|"
    r"content|influencer|territory|field|revenue|deployment|engagement|"
    r"activation|relationship|portfolio|customer|client|supply|installation|"
    r"pr|public relations"
    r")\s+(?:managers?|mgr)\b",
    re.I,
)

# True people-management signal. "Manager" counts as people management only when
# it leads the title ("Manager, X" / "Manager X") or is a recognised
# team-leadership discipline; otherwise the trailing-"Manager" form is treated
# as an IC title (see _IC_MANAGER) and falls through.
# Leading "Manager" / "Senior Manager" is people management. "Assistant Manager"
# and "Associate Manager" are deliberately excluded (the gold set treats those as
# UNKNOWN sub-manager grades).
_LEADING_MANAGER = re.compile(r"^\s*(?:senior\s+|sr\.?\s+)?managers?\b", re.I)
_PEOPLE_MANAGER = re.compile(
    r"^\s*managers?\b"  # leading "Manager ..." / "Manager, ..."
    r"|\b(?:people lead)\b"
    # team-leadership disciplines that manage people
    r"|\b(?:engineering|software engineering|data engineering|data science|"
    r"quality assurance|quality|regulatory affairs|district|payroll|"
    r"administration|design)\s+(?:managers?|mgr)\b",
    re.I,
)

_INTERN = re.compile(
    r"\b(?:intern|internship|co-?op|apprentice|werk?student|werkstudent|praktikant|praktikum)\b",
    re.I,
)

# Director runs BEFORE executive so "Head of X" and "(Assistant) Vice President"
# / AVP resolve to director rather than executive.
_DIRECTOR = re.compile(
    r"\b(?:director|head of|avp|assistant vice president)\b",
    re.I,
)

_EXECUTIVE = re.compile(
    r"\b(?:chief|c[etfoi]o|ciso|chro|cso|cro|cdo|cxo|cmo|cpo|svp|evp|vp"
    r"|vice president|president)\b",
    re.I,
)

_PRINCIPAL = re.compile(r"\b(?:principal|distinguished)\b", re.I)
# "Fellow" is overloaded: in tech it's the principal-IC rung ("Technical/Distinguished Fellow",
# "Fellow Engineer"), but in academia/medicine it's an EARLY-CAREER trainee ("Research Fellow",
# "Postdoctoral Fellow", "Clinical Fellow"). Only the explicitly-tech form maps to principal; the
# academic/medical forms are handled by _ACADEMIC (-> junior) or left unknown.
_TECH_FELLOW = re.compile(
    r"\b(?:technical|distinguished|principal|senior)\s+fellow\b|\bfellow\s+engineer\b", re.I
)
# "Member of Technical Staff" / MTS is an IC ladder, NOT staff-level. Handle before _STAFF so
# "Technical Staff" doesn't trip the plain staff rule.
_MTS = re.compile(r"\bmember of technical staff\b|\bmts\b", re.I)
_STAFF = re.compile(r"\bstaff\b", re.I)
# "Staff X" is the senior IC rung in tech/eng/product, but for many other professions "Staff" just
# means rank-and-file ("Staff Accountant", "Staff Nurse", "Staff Auditor") — NOT staff-level. These
# are denied so they fall through to UNKNOWN rather than being mislabelled senior-staff.
_STAFF_RANKFILE = re.compile(
    r"\bstaff\s+(?:accountant|nurse|auditor|pharmacist|writer|assistant|clerk|bookkeeper|"
    r"therapist|technician|attorney|paralegal|cpa|cashier|associate|receptionist|"
    r"physician|dentist|hygienist|teacher|editor|reporter)\b",
    re.I,
)
# Unambiguous title-only academic/medical conventions (the role noun fixes the level regardless of
# sector): assistant/associate professor, postdoc, etc. Checked after generic seniority words so an
# explicit "Senior/Junior" qualifier still wins.
_ACADEMIC: list[tuple[re.Pattern[str], JobLevel]] = [
    (re.compile(r"\bassistant professor\b", re.I), JobLevel.ENTRY),
    (re.compile(r"\bassociate professor\b", re.I), JobLevel.MID),
    (re.compile(r"\b(?:post-?doctoral|post-?doc)\b", re.I), JobLevel.JUNIOR),
    (
        re.compile(r"\b(?:research|postdoctoral|postdoc|teaching|clinical) fellow\b", re.I),
        JobLevel.JUNIOR,
    ),
]
_LEAD = re.compile(r"\b(?:lead|tech lead|team lead)\b", re.I)
# "Mid / Senior" combos resolve to mid (lower bound), handled before SENIOR.
_MID_SENIOR = re.compile(r"\bmid\s*/\s*senior\b", re.I)
_SENIOR = re.compile(r"\b(?:senior|sr\.?|snr)\b", re.I)
_JUNIOR = re.compile(r"\b(?:junior|jr\.?|jnr)\b", re.I)

# Explicit entry-level signals.
# Sales / business / partner *development representative* titles are all
# entry-level (the role is an entry ramp), so they are matched here.
# Bare "associate" is intentionally NOT an entry signal: it is an overloaded
# job-grade word (Direct Marketing Associate, Investment Banking Associate, ...)
# that the gold set does not treat as entry. Only the leading "Associate, X"
# form (early-career rotational hires) is matched.
_ENTRY = re.compile(
    r"\b(?:entry[- ]level|new ?grads?|graduate|trainee|early[- ]career"
    r"|campus (?:hire|recruit(?:ing|ment)?))\b"
    r"|^\s*associate,"
    r"|\b(?:business |partner |sales )?development representative\b"
    r"|\bsales development\b"
    r"|\b[bs]dr\b",
    re.I,
)

# Bare entry-ramp titles. A handful of professions hire the BARE title as their entry ramp
# ("Account Executive" at payroll/staffing shops, "Sales Consultant", "Financial Advisor"
# training programs, "Insurance Agent", "Leasing Consultant") — the unqualified title itself is
# the first rung. Checked LAST in the cascade, so ANY other signal wins first ("Senior Account
# Executive" -> senior, "Account Executive II" -> mid), and a market-segment qualifier
# (enterprise/mid-market/strategic/...) suppresses the inference: those variants require prior
# closing experience and are NOT entry.
_ENTRY_RAMP = re.compile(
    r"\b(?:account executive|sales consultant|financial advisor|insurance agent"
    r"|leasing consultant)\b",
    re.I,
)
_ENTRY_RAMP_DENY = re.compile(
    r"\b(?:enterprise|strategic|majors?|named|key|national|global|commercial"
    r"|mid[- ]?market|smb|experienced)\b",
    re.I,
)

# --- Numeric / explicit-level tokens ----------------------------------------
_TOK_EARLY = re.compile(r"\(\s*early\b|\bearly[- ]career\b", re.I)
_TOK_MID = re.compile(r"\(\s*mid\b|\bmid[- ]level\b", re.I)
# Ladder codes like L5 / E4 / P3 / IC6 (case-sensitive to avoid false hits).
_LADDER = re.compile(r"\b(?:IC|[LEP])-?([1-9])\b")
# Roman/arabic suffix levels (word boundaries; roman is case-sensitive so "II"
# is never matched inside lowercase words).
_ROMAN_PRINCIPAL = re.compile(r"\bVI\b")  # level 6
_ROMAN_STAFF = re.compile(r"\bV\b")  # level 5 (checked after VI; \bV\b can't match inside "VI")
_ROMAN_SENIOR = re.compile(r"\b(?:III|IV)\b")
_ROMAN_MID = re.compile(r"\bII\b")
_ROMAN_ENTRY = re.compile(r"\bI\b")
_ARABIC = re.compile(r"\b([1-4])\b")

_LADDER_MAP: dict[int, JobLevel] = {
    1: JobLevel.ENTRY,
    2: JobLevel.ENTRY,
    3: JobLevel.MID,
    4: JobLevel.MID,
    5: JobLevel.SENIOR,
    6: JobLevel.STAFF,
    7: JobLevel.PRINCIPAL,
    8: JobLevel.PRINCIPAL,
    9: JobLevel.PRINCIPAL,
}

_ARABIC_MAP: dict[int, JobLevel] = {
    1: JobLevel.ENTRY,
    2: JobLevel.MID,
    3: JobLevel.SENIOR,
    4: JobLevel.SENIOR,
}


def _parse_level_token(title: str) -> JobLevel | None:
    """Parse an explicit/numeric level token (returns None when absent)."""
    if _TOK_EARLY.search(title):
        return JobLevel.ENTRY
    if _TOK_MID.search(title):
        return JobLevel.MID
    m = _LADDER.search(title)
    if m:
        return _LADDER_MAP[int(m.group(1))]
    # Normalise a non-breaking space sometimes embedded in roman suffixes
    # ("Software Engineer I​I") so the roman matcher sees "II".
    t = title.replace("​", "").replace("\xa0", " ")
    if _ROMAN_PRINCIPAL.search(t):
        return JobLevel.PRINCIPAL
    if _ROMAN_STAFF.search(t):
        return JobLevel.STAFF
    if _ROMAN_SENIOR.search(t):
        return JobLevel.SENIOR
    if _ROMAN_MID.search(t):
        return JobLevel.MID
    if _ROMAN_ENTRY.search(t):
        return JobLevel.ENTRY
    m = _ARABIC.search(t)
    if m:
        return _ARABIC_MAP[int(m.group(1))]
    return None


def _is_people_manager(t: str) -> bool:
    """True when a "manager" token denotes people management, not an IC role."""
    if not re.search(r"\b(?:managers?|mgr|supervisors?|people lead)\b", t, re.I):
        return False
    # "Assistant/Associate Manager" are sub-manager grades the gold set leaves
    # as UNKNOWN, not people-management levels.
    if re.search(r"\b(?:assistant|associate)\s+managers?\b", t, re.I):
        return False
    # "Manager" as the leading head noun ("Manager, X" / "Manager X" /
    # "Senior Manager" / "Assistant Manager") is always people management.
    if _LEADING_MANAGER.search(t):
        return True
    if _PEOPLE_MANAGER.search(t):
        return True
    # Any other trailing "manager" that is NOT a recognised IC function is
    # treated as people management (supervisor/team lead default).
    return not _IC_MANAGER.search(t)


def infer_level(title: str) -> JobLevel:
    """Infer seniority from a job title. Returns UNKNOWN when no signal is present."""
    t = title or ""

    if _INTERN.search(t):
        return JobLevel.INTERN
    if _DIRECTOR.search(t):
        return JobLevel.DIRECTOR
    if _EXECUTIVE.search(t):
        return JobLevel.EXECUTIVE
    # Leading / bare "Manager" (incl. "Senior Manager", "Assistant Manager")
    # is people management and wins over the seniority word.
    if _LEADING_MANAGER.search(t):
        return JobLevel.MANAGER
    # People-management manager wins over seniority words, but trailing IC
    # "<function> Manager" titles fall through so the seniority word wins.
    if _is_people_manager(t):
        return JobLevel.MANAGER
    if _PRINCIPAL.search(t) or _TECH_FELLOW.search(t):
        return JobLevel.PRINCIPAL
    if _MTS.search(t):
        # IC level: default mid, but honor an explicit senior qualifier.
        return JobLevel.SENIOR if _SENIOR.search(t) else JobLevel.MID
    if _STAFF.search(t) and not _STAFF_RANKFILE.search(t):
        return JobLevel.STAFF
    if _LEAD.search(t):
        return JobLevel.LEAD
    if _MID_SENIOR.search(t):
        return JobLevel.MID
    if _SENIOR.search(t):
        return JobLevel.SENIOR
    if _JUNIOR.search(t):
        return JobLevel.JUNIOR
    if _ENTRY.search(t):
        return JobLevel.ENTRY

    # Unambiguous academic/medical role conventions (after generic seniority words so an explicit
    # "Senior Research Fellow" still resolves to senior via _SENIOR above).
    for rx, lvl in _ACADEMIC:
        if rx.search(t):
            return lvl

    token = _parse_level_token(t)
    if token is not None:
        return token

    # Bare entry-ramp professions LAST: only fires when nothing above matched, so any explicit
    # seniority word, level token, or manager form has already won.
    if _ENTRY_RAMP.search(t) and not _ENTRY_RAMP_DENY.search(t):
        return JobLevel.ENTRY
    return JobLevel.UNKNOWN


class LevelExtractor:
    name = "level"

    def extract(self, inp: ExtractInput) -> JobLevel:
        return infer_level(inp.title)


register_extractor(LevelExtractor())
