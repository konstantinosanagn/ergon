"""Lightweight company entity-resolution for coverage auditing + discovery seeding.

Public-company *legal* names ("Cisco Systems, Inc.", "Palantir Technologies Inc") don't slug-match
our short registry keys ("cisco", "palantir"), and brand renames ("Alphabet"->google, "Meta
Platforms"->meta) need an alias. This module turns a name into a small set of normalized
**match keys** so two records for the same company collide, with precision tuned to avoid the
classic false positive (e.g. "American Airlines" vs "American Express" must NOT match).

Rules:
- Drop legal suffixes (via ``normalize_company``) and generic descriptor tokens
  (Systems/Technologies/Holdings/Group/...).
- If the core reduces to ONE token, that token is the brand key ("cisco systems" -> "cisco").
- If two+ core tokens remain, key on the FULL core and the first TWO tokens (never a bare first
  token — that's what causes "american*" collisions).
- A small hand-curated ``ALIASES`` map covers famous brand!=legal renames.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ergon_tracker.dedup import normalize_company  # noqa: E402


def _strip_accents(s: str) -> str:
    """Fold accents so e.g. "Schrödinger"/"Schrodinger" and "Telefónica" match."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _collapse(s: str) -> str:
    """All-lowercase, alphanumerics only — so space/hyphen/punctuation differences don't block an
    otherwise-exact match (registry slug "molson-coors" vs SEC "Molson Coors Beverage")."""
    return re.sub(r"[^a-z0-9]", "", _strip_accents(s).lower())

# SEC names carry share-class/state-of-incorp/parenthetical noise that breaks matching:
# "Alphabet Inc. (Class A)", "Bancorp /MD/", "Foo Corp - Class B". Strip it before normalizing so
# "Alphabet Inc. (Class A)" -> "alphabet" (then the alias maps it to google).
_DESIGNATION_RE = re.compile(
    r"\([^)]*\)|/[a-z]{2}/|\b(?:class|cl|series|ser)\s+[a-z0-9]+\b|\bclass\s*[a-z]\b",
    re.IGNORECASE,
)


def _canon(name: str) -> str:
    return _DESIGNATION_RE.sub(" ", _strip_accents(name))

# Tokens that describe a company form/industry but don't identify it — dropped before keying.
_GENERIC = frozenset(
    {
        "systems",
        "technologies",
        "technology",
        "holdings",
        "holding",
        "group",
        "international",
        "industries",
        "enterprises",
        "solutions",
        "labs",
        "laboratories",
        "partners",
        "financial",
        "pharmaceuticals",
        "pharma",
        "networks",
        "communications",
        "motors",
        "stores",
        "worldwide",
        "global",
        "services",
        "brands",
        "resources",
    }
)

# SEC-legal (normalized) -> our brand key (normalized). Only the famous renames slug-matching can't
# bridge; keep small + high-confidence.
ALIASES = {
    "alphabet": "google",
    "meta platforms": "meta",
    "advanced micro devices": "amd",
    "international business machines": "ibm",
    "jpmorgan chase": "jpmorgan",
    "the goldman sachs": "goldman sachs",
    "rtx": "raytheon",  # RTX Corp (formerly Raytheon Technologies)
}


def core_tokens(name: str) -> list[str]:
    toks = normalize_company(_canon(name)).split()
    core = [t for t in toks if t not in _GENERIC]
    return core or toks


def match_keys(name: str) -> set[str]:
    """Normalized keys a name can be matched on (intersection with another name's keys == match)."""
    norm = normalize_company(_canon(name))
    core = core_tokens(name)
    keys: set[str] = {norm}  # full normalized (with descriptors) for exact hits
    # Aliases can key off the full normalized name OR the descriptor-stripped core, since
    # generic-token stripping may remove a word the alias depends on (e.g. "international").
    for form in (norm, " ".join(core)):
        if form in ALIASES:
            keys.add(ALIASES[form])
    if not core:
        return {k for k in keys if k}
    keys.add(" ".join(core))
    if len(core) == 1:
        keys.add(core[0])  # single-word brand after stripping descriptors
    else:
        keys.add(" ".join(core[:2]))  # first two tokens — never a bare first token (avoids FPs)
    # Also emit collapsed (no-space) forms so space/hyphen differences don't block an exact match
    # ("molson coors" <-> "molsoncoors", "t mobile" <-> "tmobile"). Still EXACT on the collapsed
    # string — NOT substring — so "stripe" never matches "stripersonline".
    keys |= {_collapse(k) for k in keys}
    return {k for k in keys if k}


def build_key_index(names: object) -> set[str]:
    """Union of match keys over a collection of names — the lookup set to test membership against."""
    idx: set[str] = set()
    for name in names:  # type: ignore[attr-defined]
        idx |= match_keys(str(name))
    return idx


def is_covered(name: str, key_index: set[str]) -> bool:
    """True if any of ``name``'s match keys is present in a prebuilt key index."""
    return bool(match_keys(name) & key_index)
