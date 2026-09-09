"""Years-of-experience (YoE) extractor.

Parses a required years-of-experience signal from a posting's description (falling back
to the title). It is deliberately conservative: precision over recall. A candidate only
counts when the number is tied to a ``year(s)/yr(s)`` unit *and* either an experience cue
is nearby (e.g. "of experience", "working", "in <field>") or a clear requirement qualifier
is present (e.g. "minimum", "at least", a trailing "+", or an explicit range). Phrases that
talk about something other than experience (vesting schedules, company age, ages, "X years
ago", "the last 5 years", "25 years in business") are filtered out. When unsure, returns
``(None, None)``.

Recall extensions (each still precision-gated):

* ``YOE`` as a unit ("3+ YOE") — self-cueing, it literally means years of experience.
* Months convert to years, rounded DOWN ("18 months of experience" -> 1; "6 months" -> 0).
* A years phrase directly paired with a degree ("BS and 8 years in fintech") counts as a
  requirement even without an explicit experience cue.
* Degree-alternation ladders ("BS + 8 years OR MS + 5 years OR PhD + 2 years") take the
  MINIMUM years across the alternatives: for filtering, the number that matters is the lowest
  barrier a candidate can clear — a role satisfiable with PhD + 2 years still requires only
  2 years at minimum, so a ``max_years=2`` new-grad-ish search must not see it as an 8-year
  role (nor an ``min_years=8`` search as one it satisfies at the top).
"""

from __future__ import annotations

import re

from .base import ExtractInput, register_extractor

__all__ = ["YoeExtractor"]


# --- word -> int (zero..twenty plus the tens) --------------------------------

_WORD_NUMBERS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

# Longest first so "seventeen" wins over "seven", etc.
_WORD_ALT = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))
_NUM = rf"(?:\d{{1,2}}|\b(?:{_WORD_ALT})\b)"

# Requirement qualifiers that may precede the number.
_PREFIX = (
    r"minimum of|at minimum|at least|no less than|no more than|"
    r"more than|less than|up to|at most|over|under|"
    r"minimum|maximum|max|min\.?|between|around|about|approximately|roughly"
)

# "yoe" (years of experience) is a self-cueing unit; months are converted (floor) to years.
_UNIT = r"(?P<unit>years?|yrs?|yoe|months?)"

_PHRASE = re.compile(
    rf"(?:(?P<prefix>{_PREFIX})\s+)?"
    rf"(?P<n1>{_NUM})\s*"
    rf"(?:(?P<sep>-|–|—|to|and)\s*(?P<n2>{_NUM})\s*)?"
    rf"\+?\s*"
    rf"{_UNIT}\b"
    rf"(?:\s*(?P<suffix>\+|or\s+more|or\s+above|or\s+greater|or\s+higher|minimum|min|plus))?",
    re.IGNORECASE,
)

# Experience cues looked for around a candidate (excludes a bare "in" on purpose, so that
# future-tense phrases like "in 5 years we grew" are not treated as experience).
_CUE = re.compile(
    r"\b(?:experience|exp|working|profession\w*|industr\w*|background|"
    r"hands[\s-]?on|expertise|track\s+record|developing|building|"
    r"programming|engineering)\b",
    re.IGNORECASE,
)
# "<n> years in <field>" — an in-field cue that only counts *after* the unit.
_IN_FIELD_AFTER = re.compile(r"^\W*in\s+[a-z]", re.IGNORECASE)

# Disqualifiers: text right after the unit that means this is not about experience.
# "or older"/"of age" = age gates; "in business"/"in operation" = company age; contract/leave
# terms = engagement length (mostly month-denominated: "6 month contract", "12 months maternity").
_DQ_AFTER = re.compile(
    r"^\W*(?:ago|old|or\s+older|of\s+age|vesting|vest|cliff|warranty|lease|"
    r"sentence|imprisonment|in\s+prison|in\s+jail|running|"
    r"of\s+growth|in\s+business|in\s+operation|of\s+employment|of\s+service|"
    r"(?:'|’)?\s*(?:continuous\s+)?(?:uk\s+|us\s+)?residenc[ey]|"  # "5 years' continuous UK residence"
    r"contract|assignment|probation|maternity|parental|internship|placement)\b",
    re.IGNORECASE,
)
# Disqualifiers: text right before the number (timeframes, ages).
_DQ_BEFORE = re.compile(r"\b(?:last|past|next|within|aged?|every|for\s+the)\s+$", re.IGNORECASE)

# Prefixes that, on their own, signal a real requirement (no extra cue needed).
_REQUIRE_PREFIXES = {
    "minimum of",
    "at minimum",
    "at least",
    "no less than",
    "more than",
    "minimum",
    "min",
    "min.",
    "over",
}
# Prefixes that flip the lone number into an upper bound.
_MAX_PREFIXES = {"up to", "at most", "no more than", "less than", "under", "maximum", "max"}

# Reject implausible YoE values (ages, calendar spans, typos).
_MAX_PLAUSIBLE = 50

# A degree token right before a years phrase ("BS + 8 years", "Master's degree and 5 years"):
# the pairing itself reads as a requirement (no extra cue needed) and marks the phrase as a
# possible arm of a degree-alternation ladder.
_DEGREE_NEAR = re.compile(
    r"(?:\bb\.?s\.?c?\b|\bb\.?a\.?\b|\bm\.?s\.?c?\b|\bm\.?b\.?a\.?\b|\bph\.?\s?d\.?\b|"
    r"\bm\.d\.\b|bachelor|master|doctora\w*|degree|diploma)[^.;\n]{0,30}$",
    re.IGNORECASE,
)
# Max chars between one alternation arm's end and the next arm's start ("... OR MS + ").
_ALT_GAP = 80

# Company / product age & tenure — the years describe the ORGANIZATION, not a candidate
# requirement: "For over 35 years, we've...", "45 year track record", "30+ years of expertise, we
# combine", "5 years in a row", "3-5 years sooner", "average tenure ... is 3+ years". yoe.py is
# precision-first but these slipped through because an experience cue ("experience"/"expertise")
# sits nearby. Three signals: a tenure phrase right after the number, a company subject right after,
# and a company framing right before.
# NOTE: "of industry"/"of expertise" are deliberately NOT here — they collide with legitimate
# candidate cues ("7 years of industry experience", "5 years of expertise in X"). The company cases
# that use them ("35 years of industry excellence, we...", "30+ years of expertise, we combine") are
# caught by _COMPANY_BEFORE ("With over") / _COMPANY_NEAR ("we combine") instead.
_COMPANY_AFTER = re.compile(
    r"^\W*(?:track\s+record|of\s+(?:success|excellence|scientific|bootstrapped|"
    r"international|innovation|growth)|in\s+a\s+row|sooner|maintenance)",
    re.IGNORECASE,
)
# A company-ACHIEVEMENT verb in the SAME sentence as the number ("30+ years of expertise, we
# combine"). Restricted to achievement verbs (not recruiting boilerplate like "we offer/we're
# looking") and scoped to the current sentence, so a requirement followed by a new "We're hiring…"
# sentence is NOT vetoed.
_COMPANY_NEAR = re.compile(
    r"\bwe(?:'|’)ve\b|\bwe\s+(?:combine|help|build|connect|serve|deliver|partner|"
    r"re[-\s]?imagine|founded|pioneered)\b",
    re.IGNORECASE,
)
_COMPANY_BEFORE = re.compile(
    r"\b(?:for|with)\s+(?:over|more\s+than|nearly|almost)\s+$|\bits\s+(?:more\s+than\s+|over\s+)?$"
    r"|\bhas\s+(?:a\s+)?$|\btenure\b[^.\n]{0,30}$",
    re.IGNORECASE,
)


def _to_int(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def _arm_key(value: tuple[int | None, int | None]) -> int:
    """Sort key for alternation arms: the arm's minimum (falls back to its max-only bound)."""
    lo, hi = value
    n = lo if lo is not None else hi
    return n if n is not None else 0  # unreachable: _value never yields (None, None)


# --- German (DE) vocab --------------------------------------------------------
# German gets its own, much smaller, self-contained parse path (rather than being threaded
# through the elaborate English precision machinery above — the disqualifier/company-tenure/
# degree-ladder rules were tuned against an English corpus and don't apply here yet). This keeps
# the English path (``_parse`` below, unchanged) provably byte-identical while still landing
# real German recall for the documented patterns.
#
# unit: "Jahre(n)"; cue: Berufserfahrung|Praxiserfahrung|Erfahrung; qualifiers ("at least/from/
# over"): mindestens|mind.|min.|ab|über|wenigstens; ranges: "N-M" / "N bis M"; compound:
# "N-jährige"; vague bands: erste (Berufs)Erfahrung -> (0,2), mehrjährige -> (3,5), fundierte ->
# (3,None), langjährige -> (5,None).
_PREFIX_DE = r"mindestens|mind\.|min\.|ab|über|wenigstens"
_REQUIRE_PREFIXES_DE = {"mindestens", "mind.", "min.", "ab", "über", "wenigstens"}

# Spelled-out German cardinals ("zwei Jahre" -> 2). Scoped to zwei..zehn per the measured gap
# (small numbers spelled out in a requirement bullet); larger spelled-out numbers are vanishingly
# rare in this position and "ein(e)" is deliberately excluded (it's the indefinite article far
# more often than a spelled-out "one", so including it would risk false triggers like "ein Jahr
# Vertragslaufzeit").
_WORD_NUMBERS_DE: dict[str, int] = {
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
}
_WORD_ALT_DE = "|".join(sorted(_WORD_NUMBERS_DE, key=len, reverse=True))
_NUM_DE = rf"(?:\d{{1,2}}|\b(?:{_WORD_ALT_DE})\b)"

_PHRASE_DE = re.compile(
    rf"(?:(?P<prefix>{_PREFIX_DE})\s+)?"
    rf"(?P<n1>{_NUM_DE})\s*"
    rf"(?:(?P<sep>-|–|—|bis)\s*(?P<n2>{_NUM_DE})\s*)?"
    r"\+?\s*"
    r"(?P<unit>Jahren|Jahre|Jahr)\b",
    re.IGNORECASE,
)

_CUE_DE = re.compile(r"\b(?:Berufserfahrung|Praxiserfahrung|Erfahrung)\w*", re.IGNORECASE)

# "5-jährige (Berufs-)Erfahrung" — an adjectival compound, not the prefix+unit shape above.
_COMPOUND_DE = re.compile(r"(?P<n1>\d{1,2})[-\s]?j[aä]hrige\w*", re.IGNORECASE)

# Disqualifier guards (precision) for the vague/explicit German matches below — mirrors the
# intent of the English _DQ_*/_COMPANY_* machinery, much more narrowly since the DE path is
# deliberately small:
#   * company tenure/age: "seit über 10 Jahren Pionier", "seit 14 Jahren erfolgreichen
#     Unternehmen", "vor über 50 Jahren gegründet" — the word right before the number/prefix is
#     "seit" (temporal: since) or "vor" (temporal: ago) framing the ORGANIZATION's age.
#   * "Mit über 30 Jahren Erfahrung ... legen wir Wert" — a company-scale opener; scoped tightly
#     to the "mit über" bigram (not "mit mindestens", which is a normal candidate-requirement
#     opener: "Mit mindestens 3 Jahren Berufserfahrung punktest du").
#   * candidate age: "mindestens 18 Jahre alt"; residency: "wohnhaft ... seit 5 Jahren" (caught by
#     the "seit" before-guard already).
#   * company scale: "über 20 Jahre am Markt".
_DQ_BEFORE_DE = re.compile(r"\b(?:seit|vor)\s*$", re.IGNORECASE)
_DQ_AFTER_DE = re.compile(r"^\W*(?:alt|jung)\b|^\W*am\s+markt\b", re.IGNORECASE)
_COMPANY_MIT_UEBER_DE = re.compile(r"\bmit\s*$", re.IGNORECASE)

# Topic-object guard for the "erste (...) Erfahrung" vague band: "erste Erfahrungen mit
# CRM-Systemen" reads as tool/skill exposure, not a professional-experience requirement (unlike
# "erste Erfahrung im digitalen Marketing", "in Schichtplanung", "aus Bereichen wie ..." — all
# still valid). The differentiator observed in-corpus is specifically the "mit <tool>" object.
_TOPIC_MIT_DE = re.compile(r"^\s*mit\s+\w", re.IGNORECASE)
# "mehrere Jahre Erfahrung hast" — part of a rhetorical "whether you already have several years
# of experience OR are just starting out" concession, not a requirement statement.
_CONCESSION_HAST_DE = re.compile(r"^\s*(?:hast|habt|haben|hat)\b", re.IGNORECASE)

# Vague experience bands, checked only when no explicit number matched. Each entry is
# (pattern, band, after-guard-or-None); when the after-guard matches the text right after the
# match, the band is skipped (not a requirement).
_VAGUE_DE: tuple[
    tuple[re.Pattern[str], tuple[int | None, int | None], re.Pattern[str] | None], ...
] = (
    # up to 3 intervening adjectives: "Erste relevante praktische Erfahrung".
    (
        re.compile(r"erste[nrs]?\s+(?:[a-zäöüß]+\s+){0,3}(?:berufs)?erfahrung\w*", re.IGNORECASE),
        (0, 2),
        _TOPIC_MIT_DE,
    ),
    # an open-ended floor, not a 3-5 band — the corpus reads "mehrjährige" as "several years and
    # up", not a capped range.
    (re.compile(r"mehrjährige\w*", re.IGNORECASE), (3, None), None),
    (re.compile(r"mehrere\s+jahre\s+erfahrung\w*", re.IGNORECASE), (3, None), _CONCESSION_HAST_DE),
    # requires the compound "Berufserfahrung", not bare "Erfahrung" — "Fundierte Erfahrung in der
    # bayerischen Küche" / "... in den Bereichen Haustechnik ..." are topic objects, not a
    # professional-experience requirement (bare "Erfahrung" never earns this band on its own).
    (re.compile(r"fundierte\w*\s+berufserfahrung\w*", re.IGNORECASE), (3, None), None),
    # requires an explicit "Erfahrung" follower — bare "langjährige Tradition"/"langjährige
    # Kunden" describes the COMPANY or something else entirely, not a candidate requirement.
    (re.compile(r"langjährige\w*\s+(?:berufs)?erfahrung\w*", re.IGNORECASE), (5, None), None),
)


def _to_int_de(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS_DE.get(token)


# --- French (FR) vocab ---------------------------------------------------------
# unit: "an(s)"/"année(s)"; qualifiers ("at least/from/over"): minimum (de)|au moins|à partir
# de|dès|plus de; ranges: "N-M ans" / "de N à M ans" / "entre N et M ans"; vague bands:
# expérience significative -> (1,3), première expérience/débutant (accepté) -> (0,2), confirmé ->
# (4,None).
#
# CRITICAL: "Bac+N" (see degree.py) is a DEGREE-LEVEL marker, never a years-of-experience count —
# a hard veto rejects any match whose number sits immediately after "Bac+"/"Bac +".
#
# Unlike the German path, an explicit range here does NOT auto-qualify as a requirement on its
# own: real postings pair age ranges with "ans" too ("Nous accueillons des enfants de 3 à 11
# ans"), so every match (ranged or not) still needs either a require-prefix ("minimum", "au
# moins", "plus de", ...) or an experience cue (expérience) nearby.
_PREFIX_FR = r"minimum de|au moins|à partir de|dès|plus de|minimum|de|entre"
_REQUIRE_PREFIXES_FR = {"minimum de", "au moins", "à partir de", "dès", "plus de", "minimum"}

_WORD_NUMBERS_FR: dict[str, int] = {
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "six": 6,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
}
_WORD_ALT_FR = "|".join(sorted(_WORD_NUMBERS_FR, key=len, reverse=True))
_NUM_FR = rf"(?:\d{{1,2}}|\b(?:{_WORD_ALT_FR})\b)"

_PHRASE_FR = re.compile(
    rf"(?:(?P<prefix>{_PREFIX_FR})\s+)?"
    rf"(?P<n1>{_NUM_FR})\s*"
    rf"(?:(?P<sep>-|–|—|à|et)\s*(?P<n2>{_NUM_FR})\s*)?"
    r"\+?\s*"
    r"(?P<unit>ann[ée]es?|ans?)\b",
    re.IGNORECASE,
)

_CUE_FR = re.compile(r"\b(?:expérience|exp)\w*", re.IGNORECASE)

# "Bac+3", "Bac + 5" — an academic-level marker, not an experience count.
_BAC_BEFORE_FR = re.compile(r"\bbac\s*\+\s*$", re.IGNORECASE)

# Company/product age & tenure ("Depuis plus de 80 ans", "il y a 12 ans, créée...", "50 ans
# d'histoire") — the years describe the ORGANIZATION, not a candidate requirement.
_DQ_BEFORE_FR = re.compile(
    r"\b(?:depuis|il\s+y\s+a|cr[ée][ée]e?\s+il\s+y\s+a|fond[ée]e?\s+il\s+y\s+a|ayant)\s*$",
    re.IGNORECASE,
)
_DQ_AFTER_FR = re.compile(
    r"^\W*(?:d['’]histoire\b|d['’]existence\b|d['’]anciennet[ée]\b|de\s+savoir[-\s]faire\b|"
    r"au\s+compteur\b|renouvelable\b|d['’]âge\b|r[ée]volus\b|d['’]antiquit[ée]\b)",
    re.IGNORECASE,
)

_VAGUE_FR: tuple[
    tuple[re.Pattern[str], tuple[int | None, int | None], re.Pattern[str] | None], ...
] = (
    # "première expérience significative" — a combined phrase; the entry-level "première" reading
    # wins (checked before the bare "expérience significative" band below), matching the corpus's
    # human labeling.
    (re.compile(r"premi[èe]re\s+expérience\w*|jeune\s+diplômé\w*", re.IGNORECASE), (0, 2), None),
    (
        re.compile(
            r"pas\s+(?:\S+\s+){0,4}d['’]expérience\b|peu\s+ou\s+pas\s+d['’]expérience\b",
            re.IGNORECASE,
        ),
        (0, 0),
        None,
    ),
    (
        re.compile(r"débutant\w*\s*(?:\(e\)\s*)?accept\w*|débutants?\s+bienvenus?", re.IGNORECASE),
        (0, 2),
        None,
    ),
    (re.compile(r"expérience\s+significative\w*", re.IGNORECASE), (1, 3), None),
    (re.compile(r"expérience\s+(?:r[ée]ussie|solide)\w*", re.IGNORECASE), (1, 3), None),
    (re.compile(r"confirmé\w*", re.IGNORECASE), (4, None), None),
)

# "vous justifiez d'une expérience d'au moins un an" — "un" (one) is otherwise excluded from
# ``_WORD_NUMBERS_FR`` (like German excludes "ein": it is far more often the indefinite article),
# but "un an" directly after an explicit require-prefix is unambiguous.
_ONE_YEAR_FR = re.compile(
    r"(?:au\s+moins|minimum(?:\s+de)?|à\s+partir\s+de|dès)\s+un\s+an\b", re.IGNORECASE
)


def _to_int_fr(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS_FR.get(token)


# --- Spanish (ES) vocab ---------------------------------------------------------
# unit: "años"; cue: experiencia (laboral|profesional)|trayectoria; qualifiers: mínimo (de)|al
# menos|más de|+N años; ranges: "entre N y M años" / "de N a M años"; negatives: "sin
# experiencia"/"no se requiere experiencia" -> 0.
#
# Unlike French, an explicit range HERE does count as a requirement on its own (no cue needed) —
# EXCEPT when "edad" (age) sits nearby, which flags the range as a candidate-age bracket
# ("entre 25 y 35 años de edad") rather than an experience range.
_PREFIX_ES = r"experiencia mínima de|mínimo de|al menos|más de|mínimo|entre|de"
_REQUIRE_PREFIXES_ES = {"experiencia mínima de", "mínimo de", "al menos", "más de", "mínimo"}

_WORD_NUMBERS_ES: dict[str, int] = {
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
}
_WORD_ALT_ES = "|".join(sorted(_WORD_NUMBERS_ES, key=len, reverse=True))
_NUM_ES = rf"(?:\d{{1,2}}|\b(?:{_WORD_ALT_ES})\b)"

_PHRASE_ES = re.compile(
    rf"(?:(?P<prefix>{_PREFIX_ES})\s+)?"
    rf"\(?(?P<n1>{_NUM_ES})\)?\s*"
    rf"(?:(?P<sep>-|–|—|y|a)\s*\(?(?P<n2>{_NUM_ES})\)?\s*)?"
    r"\+?\s*"
    r"(?P<unit>años?)\b"
    r"(?P<half>\s*y\s+medio)?",
    re.IGNORECASE,
)

_CUE_ES = re.compile(r"\b(?:experiencia|trayectoria)\w*", re.IGNORECASE)

# Age context ("entre 25 y 35 años de edad") — a bare/range number near "edad" is a candidate-age
# bracket, never an experience requirement, regardless of prefix/range shape.
_AGE_NEAR_ES = re.compile(r"\bedad\b", re.IGNORECASE)

# Company age/tenure/history ("compañía con más de 40 años de historia", "Centro ... con más de
# 25 años de experiencia", "EMBARBA es una empresa que va a cumplir con 60 años") — a company noun
# followed (within a handful of words) by "con" right before the number.
_COMPANY_BEFORE_ES = re.compile(
    r"\b(?:empresa|compañ[ií]a|centro|despacho|firma|grupo|negocio|academia)\w*"
    r"(?:\s+\S+){0,8}\s+con\s*$",
    re.IGNORECASE,
)
_DQ_AFTER_ES = re.compile(
    r"^\W*(?:de\s+historia\b|de\s+antig[üu]edad\b|ayudando\b|al\s+servicio\b|de\s+recorrido\b)",
    re.IGNORECASE,
)
# A company-growth clause in the SAME sentence ("Tras 28 años de experiencia, Grupo Intermedio ha
# ido creciendo hasta...") — the years frame the COMPANY's history, not a candidate requirement.
_COMPANY_ACHIEVEMENT_ES = re.compile(
    r"\bha\s+ido\s+creciendo\b|\bse\s+ha\s+consolidado\b|\bhemos\s+crecido\b|"
    r"\bnos\s+hemos\s+convertido\b",
    re.IGNORECASE,
)

_VAGUE_ES: tuple[
    tuple[re.Pattern[str], tuple[int | None, int | None], re.Pattern[str] | None], ...
] = (
    (re.compile(r"sin\s+experiencia\s*(?:previa)?\b", re.IGNORECASE), (0, None), None),
    (
        re.compile(r"no\s+(?:es\s+necesari[ao]|se\s+requiere)\s+experiencia\b", re.IGNORECASE),
        (0, None),
        None,
    ),
    (
        re.compile(r"reci[ée]n\s+(?:titulad[ao]|egresad[ao]|graduad[ao])\w*", re.IGNORECASE),
        (0, 2),
        None,
    ),
)


def _to_int_es(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS_ES.get(token)


class YoeExtractor:
    """Extract ``(min_years, max_years)`` of required experience from a posting."""

    name = "yoe"

    def extract(self, inp: ExtractInput) -> tuple[int | None, int | None]:
        """Return ``(min_years, max_years)``; ``(None, None)`` when nothing found."""
        lang = inp.language or "en"
        for text in (inp.description_text, inp.title):
            if not text:
                continue
            result = self._parse(text, lang)
            if result != (None, None):
                return result
        return (None, None)

    def _parse(self, text: str, lang: str = "en") -> tuple[int | None, int | None]:
        if lang == "de":
            return self._parse_de(text)
        if lang == "fr":
            return self._parse_fr(text)
        if lang == "es":
            return self._parse_es(text)
        return self._parse_en(text)

    def _parse_de(self, text: str) -> tuple[int | None, int | None]:
        for m in _PHRASE_DE.finditer(text):
            value = self._value_de(m)
            if value is None:
                continue
            if self._is_valid_de(text, m):
                return value
        m2 = _COMPOUND_DE.search(text)
        if m2 is not None:
            n = _to_int(m2.group("n1"))
            if n is not None and n <= _MAX_PLAUSIBLE:
                return (n, None)
        for pattern, band, after_guard in _VAGUE_DE:
            m3 = pattern.search(text)
            if m3 is None:
                continue
            if after_guard is not None and after_guard.match(text[m3.end() : m3.end() + 20]):
                continue
            return band
        return (None, None)

    @staticmethod
    def _value_de(m: re.Match[str]) -> tuple[int | None, int | None] | None:
        n1 = _to_int_de(m.group("n1"))
        if n1 is None or n1 > _MAX_PLAUSIBLE:
            return None
        raw_n2 = m.group("n2")
        if raw_n2 is not None:
            n2 = _to_int_de(raw_n2)
            if n2 is None or n2 > _MAX_PLAUSIBLE:
                return None
            lo, hi = (n1, n2) if n1 <= n2 else (n2, n1)
            return (lo, hi)
        return (n1, None)  # every German qualifier here reads as an open-ended minimum

    @staticmethod
    def _is_valid_de(text: str, m: re.Match[str]) -> bool:
        before = text[max(0, m.start() - 40) : m.start()]
        after = text[m.end() : m.end() + 45]
        # Hard vetoes first — company tenure/age, candidate age, residency — checked before the
        # prefix short-circuit below, since "über"/"mindestens" are themselves valid requirement
        # prefixes that would otherwise rescue these ("seit über 10 Jahren", "seit mindestens 5
        # Jahren", "mindestens 18 Jahre alt").
        if _DQ_BEFORE_DE.search(before) or _DQ_AFTER_DE.match(after):
            return False
        prefix = (m.group("prefix") or "").strip().lower()
        # "Mit über 30 Jahren Erfahrung ... legen wir" — company-scale opener. Scoped to the
        # "mit über" bigram so "Mit mindestens 3 Jahren Berufserfahrung" (a normal candidate
        # requirement) is untouched.
        if prefix == "über" and _COMPANY_MIT_UEBER_DE.search(before):
            return False
        if prefix in _REQUIRE_PREFIXES_DE:
            return True
        if m.group("n2") is not None:  # an explicit range reads as a requirement on its own
            return True
        return bool(_CUE_DE.search(before) or _CUE_DE.search(after))

    def _parse_fr(self, text: str) -> tuple[int | None, int | None]:
        for m in _PHRASE_FR.finditer(text):
            value = self._value_fr(m)
            if value is None:
                continue
            if self._is_valid_fr(text, m):
                return value
        if _ONE_YEAR_FR.search(text):
            return (1, None)
        for pattern, band, after_guard in _VAGUE_FR:
            m2 = pattern.search(text)
            if m2 is None:
                continue
            if after_guard is not None and after_guard.match(text[m2.end() : m2.end() + 20]):
                continue
            return band
        return (None, None)

    @staticmethod
    def _value_fr(m: re.Match[str]) -> tuple[int | None, int | None] | None:
        n1 = _to_int_fr(m.group("n1"))
        if n1 is None or n1 > _MAX_PLAUSIBLE:
            return None
        raw_n2 = m.group("n2")
        if raw_n2 is not None:
            n2 = _to_int_fr(raw_n2)
            if n2 is None or n2 > _MAX_PLAUSIBLE:
                return None
            lo, hi = (n1, n2) if n1 <= n2 else (n2, n1)
            return (lo, hi)
        return (n1, None)

    @staticmethod
    def _is_valid_fr(text: str, m: re.Match[str]) -> bool:
        before = text[max(0, m.start() - 40) : m.start()]
        after = text[m.end() : m.end() + 45]
        if _BAC_BEFORE_FR.search(text[max(0, m.start() - 15) : m.start()]):
            return False
        if _DQ_BEFORE_FR.search(before) or _DQ_AFTER_FR.match(after):
            return False
        prefix = (m.group("prefix") or "").strip().lower()
        if prefix in _REQUIRE_PREFIXES_FR:
            return True
        return bool(_CUE_FR.search(before) or _CUE_FR.search(after))

    def _parse_es(self, text: str) -> tuple[int | None, int | None]:
        for m in _PHRASE_ES.finditer(text):
            value = self._value_es(m)
            if value is None:
                continue
            if self._is_valid_es(text, m):
                return value
        for pattern, band, after_guard in _VAGUE_ES:
            m2 = pattern.search(text)
            if m2 is None:
                continue
            if after_guard is not None and after_guard.match(text[m2.end() : m2.end() + 20]):
                continue
            return band
        return (None, None)

    @staticmethod
    def _value_es(m: re.Match[str]) -> tuple[int | None, int | None] | None:
        n1 = _to_int_es(m.group("n1"))
        if n1 is None or n1 > _MAX_PLAUSIBLE:
            return None
        raw_n2 = m.group("n2")
        if raw_n2 is not None:
            n2 = _to_int_es(raw_n2)
            if n2 is None or n2 > _MAX_PLAUSIBLE:
                return None
            lo, hi = (n1, n2) if n1 <= n2 else (n2, n1)
            return (lo, hi)
        if m.group("half"):
            # "un año y medio" -> rounds UP (1.5 -> 2), the one documented half-year convention.
            n1 += 1
        return (n1, None)

    @staticmethod
    def _is_valid_es(text: str, m: re.Match[str]) -> bool:
        after = text[m.end() : m.end() + 45]
        window = text[max(0, m.start() - 60) : m.end() + 60]
        if _AGE_NEAR_ES.search(window):
            return False
        # A wider window for the company-subject guard: "Firma multidisciplinar formada por
        # diferentes profesionales del sector jurídico, con más de 15 años" needs ~90 chars to
        # reach back to the company noun.
        company_before = text[max(0, m.start() - 150) : m.start()]
        if _COMPANY_BEFORE_ES.search(company_before) or _DQ_AFTER_ES.match(after):
            return False
        same_sentence_after = re.split(r"[.\n;]", text[m.end() : m.end() + 90])[0]
        if _COMPANY_ACHIEVEMENT_ES.search(same_sentence_after):
            return False
        prefix = (m.group("prefix") or "").strip().lower()
        if prefix in _REQUIRE_PREFIXES_ES:
            return True
        if m.group("n2") is not None:  # explicit range — valid unless "edad" vetoed it above
            return True
        return bool(
            _CUE_ES.search(text[max(0, m.start() - 40) : m.start()]) or _CUE_ES.search(after)
        )

    def _parse_en(self, text: str) -> tuple[int | None, int | None]:
        # First valid phrase wins — unless it opens a degree-alternation ladder ("BS + 8 years
        # OR MS + 5 years OR PhD + 2 years"): then every adjacent degree-paired arm is collected
        # and the MINIMUM across arms is returned (the lowest barrier a candidate can clear).
        chain: list[tuple[int, tuple[int | None, int | None]]] = []  # (arm end, value)
        for m in _PHRASE.finditer(text):
            value = self._value(m)
            if value is None:
                continue
            paired = bool(_DEGREE_NEAR.search(text[max(0, m.start() - 40) : m.start()]))
            if not self._is_valid(text, m, degree_paired=paired):
                continue
            if not chain:
                if not paired:
                    return value  # plain requirement: first valid phrase, as before
                chain.append((m.end(), value))
            elif paired and m.start() - chain[-1][0] <= _ALT_GAP:
                chain.append((m.end(), value))
            else:
                break  # next phrase isn't an adjacent arm — the ladder is over
        if chain:
            return min((v for _, v in chain), key=_arm_key)
        return (None, None)

    def _value(self, m: re.Match[str]) -> tuple[int | None, int | None] | None:
        months = m.group("unit").lower().startswith("month")
        n1 = _to_int(m.group("n1"))
        if n1 is None or (not months and n1 > _MAX_PLAUSIBLE):
            return None
        raw_n2 = m.group("n2")
        if raw_n2 is not None:
            n2 = _to_int(raw_n2)
            if n2 is None or (not months and n2 > _MAX_PLAUSIBLE):
                return None
            lo, hi = (n1, n2) if n1 <= n2 else (n2, n1)
            if months:
                lo, hi = lo // 12, hi // 12
            # "N-M+ years" / "N to M or more": the trailing "+"/"or more" opens the UPPER bound,
            # so the requirement is really a minimum of N (not a cap at M) — "6-10+ years" means
            # "6 or more". Without this we wrongly report (6, 10).
            suffix = (m.group("suffix") or "").lower()
            if "+" in m.group(0) or suffix in {
                "or more",
                "or above",
                "or greater",
                "or higher",
                "plus",
                "minimum",
                "min",
            }:
                return (lo, None)
            return (lo, hi)
        if months:
            n1 //= 12  # months -> whole years, rounded down ("18 months" -> 1)
        prefix = (m.group("prefix") or "").strip().lower()
        if prefix in _MAX_PREFIXES:
            return (None, n1)
        return (n1, None)

    def _is_valid(self, text: str, m: re.Match[str], *, degree_paired: bool = False) -> bool:
        before = text[max(0, m.start() - 40) : m.start()]
        after = text[m.end() : m.end() + 45]

        # Hard vetoes: phrase is clearly not about experience.
        if _DQ_AFTER.match(after) or _DQ_BEFORE.search(before):
            return False
        # Company/product age & tenure (org's years, not a candidate requirement). Only the
        # before-framing ("for over N years") and after-tenure phrases ("N year track record",
        # "N years of success") are safe vetoes; a "we've/our" merely NEAR the number is NOT — real
        # requirements are routinely followed by company boilerplate ("5+ years experience. We're…").
        if _COMPANY_AFTER.match(after) or _COMPANY_BEFORE.search(before):
            return False
        # A company-achievement clause in the SAME sentence ("30+ years of expertise, we combine").
        same_sentence_after = re.split(r"[.\n;]", text[m.end() : m.end() + 70])[0]
        if _COMPANY_NEAR.search(same_sentence_after):
            return False
        # "for/with/just over N years" — the "over"/"more than" is absorbed as the phrase prefix, so
        # the company framing ("For over 45 years, …", "with over 20 years of experience") sits right
        # before it. Company/product tenure, not a candidate requirement — veto before the cue/prefix
        # acceptance below (otherwise a trailing "of experience" or the "over" prefix accepts it).
        _pfx = (m.group("prefix") or "").strip().lower()
        if _pfx in {"over", "more than", "nearly", "almost"} and re.search(
            r"\b(?:for|with|just)\s*$", before, re.IGNORECASE
        ):
            return False
        # Months are almost never a YoE *requirement* (they're contracts/training/timelines/
        # probation). Accept only when an experience cue sits immediately after ("18 months of
        # experience"); otherwise veto ("6-12 months looks like...", "3 month training").
        if m.group("unit").lower().startswith("month") and not (
            _CUE.search(after[:25]) or _IN_FIELD_AFTER.match(after)
        ):
            return False

        # "YOE" spells out years-of-experience; a degree+years pairing reads as a requirement.
        if m.group("unit").lower() == "yoe" or degree_paired:
            return True

        if _CUE.search(before) or _CUE.search(after) or _IN_FIELD_AFTER.match(after):
            return True

        # No cue — accept only when the phrase itself reads as a requirement.
        prefix = (m.group("prefix") or "").strip().lower()
        if prefix in _REQUIRE_PREFIXES:
            return True
        if m.group("n2") is not None:  # an explicit range
            return True
        if m.group("suffix") is not None:
            return True
        return "+" in m.group(0)


register_extractor(YoeExtractor())
