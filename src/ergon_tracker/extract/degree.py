"""Minimum-degree-requirement extractor (deterministic gazetteer + regex, no ML).

Parses what a posting says about education and reduces it to two fields:

* ``degree_min`` — the MINIMUM degree level that satisfies the posting, one of
  ``highschool < associate < bachelor < master < phd_md`` (``None`` = not stated).
  When several degrees appear ("BS required, MS preferred"; "PhD, or BS with 5+ years")
  the minimum level that satisfies wins — that is the barrier a candidate actually faces.
* ``degree_required`` — tri-state scope, mirroring ``sponsorship.py``:
  ``True`` = stated as required, ``False`` = preferred-only / "or equivalent experience",
  ``None`` = degree mentioned but scope unstated. NOTE: "strongly preferred" is still
  ``False`` — the William Blair case ("M.D. or Ph.D. ... strongly preferred") reports
  ``("phd_md", False)`` so consumers see the nuance, while the ``max_degree`` filter
  (see ``SearchQuery``) still excludes it for a bachelor-capped search.

Precision-first, like every extractor here (published systems hit ~94.5% on level but only
~74% on required-vs-preferred, so scope stays conservative):

* Bare "degree" never matches — only gazetteer terms do — so "high degree of autonomy",
  "360 degree feedback" and temperatures can't fire.
* Dot-less abbreviations (BS/BA/MS/MA) match only in a degree context ("BS in Physics",
  "BS/MS", "MS degree"), never inside "MS SQL Server 2019" or "MS Office".
* Mentions in tuition-reimbursement / benefits sentences are ignored entirely.
* Scope comes from the mention's own sentence/bullet first (preferred beats required when
  both cues appear, and "or equivalent" always downgrades to ``False`` — a degree-less
  candidate is not excluded); only then from the nearest section header
  ("Qualifications"/"Requirements" -> required, "Preferred"/"Nice to have" -> preferred).
"""

from __future__ import annotations

import re

from ..models import DEGREE_LEVELS, DEGREE_ORDER
from .base import ExtractInput, _vocab, register_extractor

__all__ = ["DegreeExtractor", "degree_from_ats_vocab"]

_RANK = DEGREE_ORDER  # rank in the canonical highschool<associate<bachelor<master<phd_md ladder

# --- gazetteer ---------------------------------------------------------------
# Each pattern maps a degree mention to its level. Dotted abbreviations are safe standalone
# (B.S. / M.S. / Ph.D. / M.D.); dot-LESS ones (BS/MS/BA) are ambiguous ("MS Office", "BA" the
# role) and require a degree context: "<abbr> in <field>", "<abbr> degree", a slash-alternation
# ("BS/MS"), or an or-list ("BS, MS or PhD"). Dot-less "MA" never matches at all (it's the
# Massachusetts abbreviation in "Boston, MA or remote"); dotted "M.A." still does.
_ABBR_NEXT = r",\s*(?:BS|BA|MS|MSc|MEng|MBA|Ph\.?D)\b"  # comma-list arm: "BS, MS or PhD"
_CTX_BS = (
    rf"(?=\s*(?:/|,?\s*or\b|,?\s*and\b|in\b|degree\b|with\b|required\b|preferred\b)|{_ABBR_NEXT})"
)
# no and/with for MS ("MS Office and ...", "familiar with MS Word")
_CTX_MS = rf"(?=\s*(?:/|,?\s*or\b|in\b|degree\b|required\b|preferred\b)|{_ABBR_NEXT})"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # doctoral / professional doctorates
    (re.compile(r"\bph\.?\s?d\.?\b", re.I), "phd_md"),
    (re.compile(r"\bdoctor(?:ate|al)\b", re.I), "phd_md"),
    (re.compile(r"\bm\.d\.?(?=[\s,/)]|$)", re.I), "phd_md"),  # M.D. needs its dots (MD = Maryland)
    (re.compile(r"\bMD\s*/\s*Ph\.?D\b"), "phd_md"),
    (re.compile(r"\bpharm\.?\s?d\.?\b", re.I), "phd_md"),
    (re.compile(r"\bd\.?v\.?m\.?\b(?=[\s,./)]|$)", re.I), "phd_md"),
    (re.compile(r"\bj\.d\.?(?=[\s,/)]|$)", re.I), "phd_md"),
    (re.compile(r"\bjuris\s+doctor\b", re.I), "phd_md"),
    # Professional-doctorate degrees named by field ("Medical degree (MD or DO)", "Law degree (JD)")
    # — the bare MD/DO/JD often lack the dots the abbreviations require, so anchor on the phrase.
    (re.compile(r"\b(?:medical|law|dental|veterinary)\s+degree\b", re.I), "phd_md"),
    # master's ("master" needs its 's or of/degree so "Scrum Master" can never match). The 's form
    # additionally requires a degree-context follower so "Master's instructions" (a ship's-master
    # rank), "Master's students" (enrolled, not held) and other possessive idioms don't fire.
    (
        re.compile(
            r"\bmaster(?:'|’)s\b(?=\s*(?:degree|of\b|in\b|or\b|level\b|qualification|"
            r"required|preferred|strongly|,|/|\.|;|:|\)|-|–|$))",
            re.I,
        ),
        "master",
    ),
    # "Master of <academic subject>" — whitelisted so "Master of Schedule/Production/your destiny"
    # (wordplay) can't match; plus the plain "master(s) degree" phrasing.
    (
        re.compile(
            r"\bmaster\s+of\s+(?:science|arts|business|engineering|public\s+health|fine\s+arts|"
            r"philosophy|laws?|education|social\s+work|music|architecture|divinity|technology|"
            r"computer\s+science|data\s+science|research|account(?:ing|ancy)|finance|nursing|"
            r"management)\b|\bmaster(?:'|’)?s?\s+degree\b",
            re.I,
        ),
        "master",
    ),
    (re.compile(r"\bmba\b", re.I), "master"),
    (re.compile(r"\bm\.\s?(?:sc|eng|s|a)\.?(?=[\s,/)]|$)", re.I), "master"),
    (re.compile(r"\bMSc\b"), "master"),  # case-sensitive: the academic form, not "MSC" (a company)
    (re.compile(rf"\b(?:MS|MEng){_CTX_MS}"), "master"),
    (re.compile(r"(?<=/)(?:MS|MEng)\b"), "master"),
    (re.compile(r"\b(?:advanced|graduate|post[-\s]?graduate)\s+degree\b", re.I), "master"),
    # bachelor's
    (re.compile(r"\bbachelor(?:'|’)?s?\b", re.I), "bachelor"),
    (re.compile(r"\bbaccalaureate\b", re.I), "bachelor"),
    (re.compile(r"\bb\.\s?(?:sc|eng|s|a)\.?(?=[\s,/)]|$)", re.I), "bachelor"),
    (re.compile(r"\bBSc\b"), "bachelor"),  # case-sensitive academic form (not an all-caps acronym)
    (re.compile(rf"\b(?:BS|BA|BEng){_CTX_BS}"), "bachelor"),
    (re.compile(r"(?<=/)(?:BS|BA|BEng)\b"), "bachelor"),
    (re.compile(r"\b(?:undergraduate|university|college)\s+degree\b", re.I), "bachelor"),
    (
        re.compile(r"\bdegree[-\s]+(?:educated|qualified)\b", re.I),
        "bachelor",
    ),  # British "degree educated"
    # "4-year degree" allowing an intervening field ("4-year computer science degree").
    (
        re.compile(r"\b(?:4|four)[-\s]year\s+(?:[A-Za-z][A-Za-z&/]*\s+){0,3}degree\b", re.I),
        "bachelor",
    ),
    # associate
    (re.compile(r"\bassociate(?:'|’)?s?\s+degree\b", re.I), "associate"),
    (re.compile(r"\ba\.\s?(?:a|s)\.?\s+degree\b", re.I), "associate"),
    # high school
    (re.compile(r"\bhigh\s?school\s+(?:diploma|degree|education)\b", re.I), "highschool"),
    (re.compile(r"\bged\b", re.I), "highschool"),
)

# --- German (DE) gazetteer -----------------------------------------------------
# Deliberately does NOT include Ausbildung/Berufsausbildung/Lehre/Meister/Techniker: these are
# VOCATIONAL qualifications, a false friend for "degree" in English, and the ``DEGREE_LEVELS``
# ladder (highschool<associate<bachelor<master<phd_md`) has no vocational rung to put them on —
# so they are simply left unmatched, which resolves to ``(None, None)`` rather than leaking into
# "bachelor". "Doktorand" (a PhD *candidate*, not a holder) is excluded from the doctorate match
# via a negative lookahead.
_PATTERNS_DE: tuple[tuple[re.Pattern[str], str], ...] = (
    # doctoral
    (re.compile(r"\bpromotion\b", re.I), "phd_md"),
    (re.compile(r"\bdoktor(?!and)\w*\b", re.I), "phd_md"),
    (re.compile(r"\bdr\.(?:\s?(?:rer|med|jur|phil)\.)?", re.I), "phd_md"),
    # master's (incl. the false-friend "Diplom"/"Dipl.-Ing.", which is a master's-equivalent)
    (re.compile(r"\bmaster(?:studium|abschluss|s)?\b", re.I), "master"),
    (re.compile(r"\bm\.\s?(?:sc|a|eng)\.?(?=[\s,/)]|$)", re.I), "master"),
    (re.compile(r"\bmagister\b", re.I), "master"),
    (re.compile(r"\bdiplom(?:studium)?\b", re.I), "master"),
    (re.compile(r"\bdipl\.?[-\s]?ing\.?\b", re.I), "master"),
    # bachelor's / Studium (bare "Studium" defaults to a first degree = bachelor). The boundary is
    # loosened to also match compound field-prefixed forms ("Jurastudium", "Informatikstudium")
    # — any run of word characters ending in "studium" — plus "Studienabschluss" (a first-degree
    # completion, distinct from the "studium" stem above).
    (re.compile(r"\bbachelor(?:studium|abschluss)?\b", re.I), "bachelor"),
    (re.compile(r"\bb\.\s?(?:sc|a|eng)\.?(?=[\s,/)]|$)", re.I), "bachelor"),
    (re.compile(r"\bhochschulabschluss\b", re.I), "bachelor"),
    (re.compile(r"\bfh-abschluss\b", re.I), "bachelor"),
    # negative lookahead excludes "master"/"diplom"/"bachelor"-prefixed compounds — those already
    # have their own dedicated (higher-level, or explicitly-bachelor) patterns above, so letting
    # this broad arm match them too would add a spurious duplicate *bachelor*-rank mention that
    # could wrongly drag e.g. a "Masterstudium"-only posting's minimum down to bachelor.
    (re.compile(r"\b(?!master|diplom|bachelor)\w*studium\b", re.I), "bachelor"),
    (re.compile(r"\bstudienabschluss\w*\b", re.I), "bachelor"),
    # high school
    (re.compile(r"\babitur\b", re.I), "highschool"),
    (re.compile(r"\bfachabitur\b", re.I), "highschool"),
    (re.compile(r"\bhauptschulabschluss\b", re.I), "highschool"),
    (re.compile(r"\bmittlere\s+reife\b", re.I), "highschool"),
    (re.compile(r"\brealschulabschluss\b", re.I), "highschool"),
)

# --- French (FR) gazetteer -----------------------------------------------------
# RNCP/Bac+N system. CAP/BEP are deliberately excluded — vocational, not on the academic
# highschool<associate<bachelor<master<phd_md ladder — so they resolve to unmatched (None), the
# same treatment German gives Ausbildung/Lehre. Bare "ingénieur" is deliberately excluded too:
# unlike "diplôme d'ingénieur"/"école d'ingénieurs" (the credential), it is overwhelmingly a JOB
# TITLE in French postings ("Ingénieur DevOps") — matching it bare would be as wrong as English
# matching bare "engineer".
_PATTERNS_FR: tuple[tuple[re.Pattern[str], str], ...] = (
    # doctoral
    (re.compile(r"\bdoctorat\w*\b", re.IGNORECASE), "phd_md"),
    (re.compile(r"\bhdr\b"), "phd_md"),
    (re.compile(r"\bbac\s*\+\s*(?:8|9|1\d)\b", re.IGNORECASE), "phd_md"),
    # master's (incl. the RNCP Bac+5/6/7 band and the pre-LMD DESS/DEA diplomas)
    # excludes "Scrum Master" (job-title false friend, same collision English handles).
    (re.compile(r"(?<!scrum\s)\bmaster\b(?!\s*class)\w*", re.IGNORECASE), "master"),
    (re.compile(r"\bmast[èe]re\w*\b", re.IGNORECASE), "master"),
    (re.compile(r"\bdiplôme\s+d['’]ing[ée]nieurs?\b", re.IGNORECASE), "master"),
    (re.compile(r"\b[ée]cole\s+d['’]ing[ée]nieurs?\b", re.IGNORECASE), "master"),
    (re.compile(r"\bgrande\s+[ée]cole\w*\b", re.IGNORECASE), "master"),
    (re.compile(r"\bdess\b", re.IGNORECASE), "master"),
    (re.compile(r"\bdea\b"), "master"),
    (re.compile(r"\bmba\b", re.IGNORECASE), "master"),
    (re.compile(r"\bbac\s*\+\s*[567]\b", re.IGNORECASE), "master"),
    # bachelor's (Licence/BUT/maîtrise/Bac+3/Bac+4/DE-level professional diplomas)
    # "licence" is a false friend for "license/permit" ("licence Claude" a software license,
    # "licence" a driving/broadcast permit) — scoped to a degree-context follower (field name,
    # "pro(fessionnelle)", or an or-list/punctuation close), same treatment as English's dot-less
    # BS/MS abbreviations.
    (
        re.compile(
            r"\blicence\b(?=\s*(?:en\b|de\b|d['’]|pro\w*\b|,|/|\)|\.|;|:|$))", re.IGNORECASE
        ),
        "bachelor",
    ),
    # "maîtrise" is an even sharper false friend: "bonne maîtrise de/des <outil/langue>" (skill
    # proficiency) vastly outnumbers the pre-Bologna "maîtrise" DEGREE in real postings. Scoped to
    # only the unambiguous degree phrasing ("titulaire d'une maîtrise", "diplôme de maîtrise",
    # or an explicit degree-list like "Licence, Master ou Maîtrise") — bare "maîtrise de <noun>" is
    # deliberately left unmatched (its dominant reading is the skill sense, not the degree).
    (
        re.compile(
            r"\b(?:titulaire\s+(?:d['’]une?\s+)?|diplôme\s+de\s+)ma[îi]trise\b", re.IGNORECASE
        ),
        "bachelor",
    ),
    # Diplôme d'État (DE) — a Bac+3-equivalent professional diploma (nursing, social work, ...).
    (
        re.compile(r"\bdiplôme\s+d['’](?:[ée]tat\s+d['’])?infirmi[èe]r\w*\b", re.IGNORECASE),
        "bachelor",
    ),
    (re.compile(r"\bBUT\b"), "bachelor"),  # case-sensitive UPPERCASE: the diploma, not "but" (goal)
    (re.compile(r"\bma[îi]trise\b", re.IGNORECASE), "bachelor"),
    (re.compile(r"\bbac\s*\+\s*[34]\b", re.IGNORECASE), "bachelor"),
    # associate (tertiary short-cycle: BTS/DUT/DEUG/Bac+2)
    (re.compile(r"\bbts\b", re.IGNORECASE), "associate"),
    (re.compile(r"\bdut\b", re.IGNORECASE), "associate"),
    (re.compile(r"\bdeug\b", re.IGNORECASE), "associate"),
    (re.compile(r"\bbac\s*\+\s*2\b", re.IGNORECASE), "associate"),
    # high school (bare Bac, not the Bac+N ladder above)
    (re.compile(r"\bbac\b(?!\s*\+)", re.IGNORECASE), "highschool"),
    (re.compile(r"\bbaccalaur[ée]at\b(?!\s*\+)", re.IGNORECASE), "highschool"),
)

_OR_EQUIV_FR = re.compile(r"\bou\s+équivalent\w*\b", re.IGNORECASE)

# --- Spanish (ES) gazetteer ------------------------------------------------------
# Region-aware (Spain + LatAm). "Bachillerato"/"ESO" (Spain secondary school) are NOT a degree —
# highschool. FP Grado Medio is deliberately excluded (vocational, no ladder rung), mirroring
# CAP/BEP above. Bare "doctor" is deliberately excluded (job title "Doctor en medicina" collision,
# same reasoning as English's dotted-only M.D. rule) — only "doctorado" (the degree noun) counts.
_PATTERNS_ES: tuple[tuple[re.Pattern[str], str], ...] = (
    # doctoral
    (re.compile(r"\bdoctorado\w*\b", re.IGNORECASE), "phd_md"),
    # master's / postgrado
    # excludes "Scrum Master" (job-title false friend, same collision English handles) and
    # "Master of the Funnel" (a gamified English job-title, not "Máster en/de <field>").
    (re.compile(r"(?<!scrum\s)\bm[áa]ster\w*\b(?!\s+of\b)", re.IGNORECASE), "master"),
    (re.compile(r"\bpost[-\s]?grado\w*\b", re.IGNORECASE), "master"),
    # bachelor's (Licenciatura/Grado universitario/Graduado/Diplomatura/título universitario)
    (re.compile(r"\blicenciatura\w*\b", re.IGNORECASE), "bachelor"),
    (re.compile(r"\bgrado\s+universitario\w*\b", re.IGNORECASE), "bachelor"),
    (re.compile(r"\bgraduado\s+escolar\b", re.IGNORECASE), "highschool"),
    (re.compile(r"\bgraduad[oa]\b(?!\s+escolar)", re.IGNORECASE), "bachelor"),
    (re.compile(r"\bdiplomatura\w*\b", re.IGNORECASE), "bachelor"),
    (
        re.compile(
            r"\bt[ií]tulo\s+(?:oficial|universitario|acad[ée]mico|de\s+grado)\b", re.IGNORECASE
        ),
        "bachelor",
    ),
    # "Ingeniería <field>"/"Ingeniero/a (en) <field>" — in Spain/LatAm postings this alone is the
    # dominant way to state a required engineering degree (a genuine false-friend risk vs. the job
    # title "ingeniero" — but unlike French "ingénieur", the measured ES corpus overwhelmingly uses
    # it as the degree marker: "Requisitos: Ingeniería Industrial, ...", "Grado en Ingeniería
    # Informática"; a bare mention drops in ``_add`` when the segment reads as a CURRENT-student
    # program (see ``_CURRENT_STUDENT_ES``), not a completed-degree requirement.
    (re.compile(r"\bingenier[ií]a\w*\b", re.IGNORECASE), "bachelor"),
    (
        re.compile(
            r"\btitulaci[oó]n\w*\b(?=\s+(?:universitaria|acad[ée]mica|oficial))", re.IGNORECASE
        ),
        "bachelor",
    ),
    # Peru/LatAm: "Bachiller"/"Grado de Bachiller" is a first-degree holder title (NOT
    # "Bachillerato", Spain's highschool-equivalent — the word-boundary regex below cannot match
    # inside "bachillerato" since it continues with "-ato").
    (re.compile(r"\bbachiller\b", re.IGNORECASE), "bachelor"),
    # associate (tertiary short-cycle: Grado Superior/Técnico Superior/TSU)
    (re.compile(r"\bgrado\s+superior\b", re.IGNORECASE), "associate"),
    (re.compile(r"\bt[ée]cnico\s+superior\w*\b", re.IGNORECASE), "associate"),
    (re.compile(r"\btsu\b", re.IGNORECASE), "associate"),
    # high school (Spain secondary — NOT the Peru "Bachiller" degree title above)
    (re.compile(r"\bbachillerato\b", re.IGNORECASE), "highschool"),
    (re.compile(r"\beso\b"), "highschool"),
)

_OR_EQUIV_ES = re.compile(r"\bo\s+equivalente\w*\b", re.IGNORECASE)

# "Estudiante universitario/graduado de carreras...", "Matrícula en la universidad", "Matrícula
# hasta mínimo 2026" — a candidate currently ENROLLED (student-internship postings), not a
# completed-degree requirement; mirrors the German "neben/nach Deinem Studium" guard.
_CURRENT_STUDENT_ES = re.compile(r"\bestudiante\w*\b|\bmatr[ií]cula\w*\b", re.IGNORECASE)

# "empresa/sector/equipo/área/perfil/proyecto(s) de ingeniería" — a FIELD/DOMAIN descriptor
# ("Empresa de ingeniería especializada en...", "perfil de ingeniería con interés en...", not a
# degree requirement for the mention itself); scoped tightly to the word right before "ingeniería"
# so a genuine requirement ("Grado en Ingeniería Industrial") is untouched.
_INGENIERIA_FIELD_DESC_ES = re.compile(
    r"\b(?:empresa|sector|equipo|[aá]rea|perfil|proyectos?)\s+de\s*$", re.IGNORECASE
)

_PATTERNS_TABLE: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "en": _PATTERNS,
    "de": _PATTERNS_DE,
    "fr": _PATTERNS_FR,
    "es": _PATTERNS_ES,
}

# German "or equivalent qualification" escape — softens scope the same way the English
# ``_OR_EQUIV`` does; reused by ``_scope`` below.
_OR_EQUIV_DE = re.compile(r"\boder\s+vergleichbare\s+qualifikation\w*\b", re.IGNORECASE)

# Vocational (Ausbildung/Lehre/Berufsausbildung) offered as an "oder"-alternative to the academic
# degree ("Ausbildung oder Studium", "Studium ... oder eine vergleichbare Berufsausbildung",
# "Physiotherapie-Ausbildung oder Bachelor"): the posting is satisfiable WITHOUT the degree, so
# the academic-degree mention is dropped entirely (no degree_min), mirroring how English's
# ``_OR_EQUIV`` softens scope but going one step further, matching this benchmark's
# reconciliation (a vocational-satisfiable posting is scored as "no degree stated").
# "neben Deinem Studium erste beruflich Eindrücke sammeln" / "nach Deinem Studium
# weiterarbeiten" — a Werkstudent(-style) ad describing the CANDIDATE's own current/future
# studies (concurrent with or after this job), not a completed-degree requirement. Scoped tightly
# to "neben|nach" immediately before the possessive so a genuine requirement like "hast dein
# Studium kürzlich ... abgeschlossen" (present-perfect: studies already completed) is untouched.
_CURRENT_STUDENT_DE = re.compile(r"\b(?:neben|nach)\s+dein\w*\s+studium\b", re.IGNORECASE)

_VOC_TOKEN_DE = re.compile(r"\b(?:berufs)?ausbildung\w*\b", re.IGNORECASE)
_DEGREE_TOKEN_DE = re.compile(r"\b(?:studium|bachelor)\w*\b", re.IGNORECASE)
_ODER_TOKEN_DE = re.compile(r"\boder\b", re.IGNORECASE)
_VOC_OR_GAP = 220  # max chars between the vocational and academic tokens


def _vocational_alternative_de(segment: str) -> bool:
    """True when ``segment`` offers a vocational alternative to the academic degree via "oder"."""
    voc_spans = [m.span() for m in _VOC_TOKEN_DE.finditer(segment)]
    if not voc_spans:
        return False
    deg_spans = [m.span() for m in _DEGREE_TOKEN_DE.finditer(segment)]
    if not deg_spans:
        return False
    oder_starts = [m.start() for m in _ODER_TOKEN_DE.finditer(segment)]
    if not oder_starts:
        return False
    for v_start, v_end in voc_spans:
        for d_start, d_end in deg_spans:
            if v_end <= d_start:
                lo, hi = v_end, d_start
            elif d_end <= v_start:
                lo, hi = d_end, v_start
            else:
                continue  # overlapping/nested tokens — not a real pairing
            if hi - lo > _VOC_OR_GAP:
                continue
            if any(lo <= o < hi for o in oder_starts):
                return True
    return False


# --- ATS "education" vocabulary -> degree_min --------------------------------
# Closed set of free-text education values some ATS widgets expose directly (e.g. Workable's
# "education" field: "High School", "Associate Degree", "Bachelor's Degree", "Master's Degree",
# "Doctorate"). Matched against a lowercased, punctuation-stripped normal form so "Associate's
# Degree" / "Associate Degree" / "ASSOCIATE DEGREE" all hit the same key. Deliberately closed:
# ambiguous ATS values ("Professional", "Vocational", "Certification") have no reliable mapping
# to a single rung of the highschool<associate<bachelor<master<phd_md ladder, so they resolve to
# None rather than guess (the description-based DegreeExtractor gets a second chance instead).
_ATS_EDUCATION_VOCAB: dict[str, str] = {
    "high school": "highschool",
    "high school diploma": "highschool",
    "ged": "highschool",
    "associate degree": "associate",
    "associates degree": "associate",
    "associate": "associate",
    "bachelor degree": "bachelor",
    "bachelors degree": "bachelor",
    "bachelor": "bachelor",
    "master degree": "master",
    "masters degree": "master",
    "master": "master",
    "mba": "master",
    "doctorate": "phd_md",
    "doctoral degree": "phd_md",
    "phd": "phd_md",
    "ph d": "phd_md",
}


def degree_from_ats_vocab(value: str | None) -> str | None:
    """Map an ATS "education" vocabulary string to a ``DEGREE_LEVELS`` value.

    Unknown, empty, or ambiguous values ("Professional", "Vocational", "Certification") return
    ``None`` — never guess. Case/punctuation-insensitive (apostrophes and periods are stripped
    before lookup, so "Associate's Degree" and "Ph.D." both match).
    """
    if not value:
        return None
    norm = re.sub(r"[’'.]", "", value.strip().lower())
    norm = " ".join(norm.split())
    return _ATS_EDUCATION_VOCAB.get(norm)


# --- scope cues (evaluated on the mention's own sentence/bullet) --------------
# "or equivalent (experience)" downgrades to preferred-only: the practical semantics is that
# a candidate WITHOUT the degree is not excluded. Checked before the required cue on purpose
# ("Bachelor's or equivalent experience required" -> False).
_OR_EQUIV = re.compile(
    r"\bor\s+equivalent\b|\bor\s+comparable\b|\bequivalent\s+(?:work\s+)?experience\b", re.I
)
# The tight "<degree> or equivalent required" phrase — a real requirement (equivalent credential is
# accepted, but something is required). The negative lookahead excludes "or equivalent EXPERIENCE
# required" (experience substitutes for the degree -> preferred-only, stays False).
_EQUIV_REQUIRED = re.compile(
    r"\bor\s+(?:equivalent|comparable)\b(?!\s+(?:work\s+)?experience)[^.\n;]{0,12}\brequired\b",
    re.I,
)
_PREFERRED = re.compile(
    r"\bpreferred\b|\ba\s+plus\b|\bnice\s+to\s+have\b|\bideally\b|\bdesir(?:ed|able)\b"
    r"|\badvantageous\b|\bbonus\b|\bnot\s+required\b",
    re.I,
)
_REQUIRED = re.compile(
    r"\brequired\b|\bmust\s+(?:have|hold|possess)\b|\bminimum\b|\bneeded\b"
    r"|\bor\s+(?:above|higher)\b|\bat\s+least\s+an?\b|\bminimum\s+of\s+an?\b",
    re.I,
)

# Benefits / tuition context: a degree mentioned here is about perks, not qualifications —
# ignore the mention entirely ("tuition reimbursement toward your degree").
_BENEFITS = re.compile(
    r"\btuition\b|\breimburse\w*|\btoward\s+(?:your|a|an)\s+degree\b|\bdegree\s+program\b"
    r"|\bcontinuing\s+education\b|\beducation\s+assistance\b",
    re.I,
)

# Section headers, scanned backwards from a mention when its own sentence has no cue.
# Nearest header wins. "Preferred"-flavored headers are checked against the same window.
_SEC_REQUIRED = re.compile(
    r"(?:minimum|basic)\s+qualifications|requirements?|qualifications"
    r"|what\s+you.{0,2}ll\s+need|must[-\s]haves?|who\s+you\s+are"
    r"|what\s+(?:we.{0,2}re\s+looking\s+for|you\s+bring)|you.{0,2}ll\s+(?:have|bring|need)"
    r"|\beducation\s*(?:&|and)?\s*(?:experience|requirements?)?\s*[:\n]",
    re.I,
)
_SEC_PREFERRED = re.compile(
    r"preferred\s+qualifications|nice[-\s]to[-\s]haves?|bonus\s+points|pluses",
    re.I,
)
_SEC_BENEFITS = re.compile(r"\bbenefits\b|\bperks\b|what\s+we\s+offer", re.I)

_SECTION_WINDOW = 600  # chars scanned backwards for a governing section header
_SEGMENT_CAP = 300  # max chars of sentence/bullet examined on each side of a mention

# Sentence/bullet boundary: a newline or bullet always ends a segment; a period only when
# followed by whitespace + an uppercase start (so "M.D. or Ph.D." doesn't split mid-mention).
_BOUNDARY = re.compile(r"[\n\r•;]|\.(?=\s+[A-Z])")

# Bare "degree" — only in an unambiguous requirement follower ("Degree in Computer Science",
# "degree or equivalent", "degree required", "degree from an accredited university", "degree
# level"). "high degree of autonomy" / "360 degree" can't match ("of"/number is not a follower).
# Defaults to bachelor (a bare degree requirement is a first degree); it is SUPPRESSED whenever a
# specific degree word immediately precedes it ("master's degree in X" already counted as master),
# so the bare arm never double-counts and drags a higher requirement down to bachelor.
_BARE_DEGREE = re.compile(
    r"\bdegree\b(?=\s+(?:in\b|or\b|required|preferred|from\b|level\b|is\s+required|is\s+preferred))",
    re.I,
)


def _segment(text: str, start: int, end: int) -> tuple[str, int]:
    """The sentence/bullet containing ``text[start:end]`` (capped at ``_SEGMENT_CAP`` per side)
    and its start offset in ``text`` (so a mention's position within the segment is known)."""
    lo = max(0, start - _SEGMENT_CAP)
    hi = min(len(text), end + _SEGMENT_CAP)
    seg_start, seg_end = lo, hi
    for m in _BOUNDARY.finditer(text, lo, hi):
        if m.end() <= start:
            seg_start = m.end()
        elif m.start() >= end:
            seg_end = m.start()
            break
    return text[seg_start:seg_end], seg_start


def _section_scope(text: str, start: int) -> bool | None:
    """Scope implied by the nearest preceding section header (None = no governing header).

    Preferred-header spans are masked before the required scan so that the "qualifications"
    inside "Preferred Qualifications" can't be misread as a required header.
    """
    window = text[max(0, start - _SECTION_WINDOW) : start]
    hits: list[tuple[int, bool]] = []
    pref_spans: list[tuple[int, int]] = []
    for m in _SEC_PREFERRED.finditer(window):
        pref_spans.append((m.start(), m.end()))
        hits.append((m.start(), False))
    for m in _SEC_REQUIRED.finditer(window):
        if not any(lo <= m.start() < hi for lo, hi in pref_spans):
            hits.append((m.start(), True))
    if not hits:
        return None
    return max(hits)[1]  # nearest (right-most) header wins


def _in_benefits_section(text: str, start: int) -> bool:
    """True when the nearest preceding header is benefits-flavored (mention must be ignored)."""
    window = text[max(0, start - _SECTION_WINDOW) : start]
    ben = [m.start() for m in _SEC_BENEFITS.finditer(window)]
    if not ben:
        return False
    qual = [m.start() for m in _SEC_REQUIRED.finditer(window)] + [
        m.start() for m in _SEC_PREFERRED.finditer(window)
    ]
    return not qual or max(ben) > max(qual)


class DegreeExtractor:
    """Extract ``(degree_min, degree_required)`` from a posting description."""

    name = "degree"

    def extract(self, inp: ExtractInput) -> tuple[str | None, bool | None]:
        """Return ``(degree_min, degree_required)``; ``(None, None)`` when no degree is stated."""
        text = inp.description_text
        if not text:
            return (None, None)
        lang = inp.language or "en"
        patterns = _vocab(lang, _PATTERNS_TABLE)
        # (rank, scope) per surviving mention; the minimum rank is the real barrier.
        mentions: list[tuple[int, bool | None]] = []
        specific_spans: list[tuple[int, int]] = []
        for pattern, level in patterns:
            for m in pattern.finditer(text):
                specific_spans.append((m.start(), m.end()))
                self._add(text, m.start(), m.end(), _RANK[level], mentions, lang)
        # Guarded bare-degree ("degree" the English word) pass, bachelor-default — English only;
        # German's bare-first-degree default is the "Studium" gazetteer entry above instead.
        # Suppressed when the "degree" token sits inside or right after a specific degree phrase
        # ("master's degree", "advanced degree", "PhD degree"), so the bare arm never double-counts
        # and drags a higher requirement down to bachelor.
        if lang == "en":
            for m in _BARE_DEGREE.finditer(text):
                if any(s <= m.start() <= e + 15 for s, e in specific_spans):
                    continue
                self._add(text, m.start(), m.end(), _RANK["bachelor"], mentions, lang)
        if not mentions:
            return (None, None)
        min_rank = min(rank for rank, _ in mentions)
        scopes = [s for rank, s in mentions if rank == min_rank]
        # Any explicit "required" at the minimum level wins; else any explicit "preferred".
        scope = True if True in scopes else (False if False in scopes else None)
        return (DEGREE_LEVELS[min_rank], scope)

    def _add(
        self,
        text: str,
        start: int,
        end: int,
        rank: int,
        mentions: list[tuple[int, bool | None]],
        lang: str = "en",
    ) -> None:
        """Process one gazetteer hit: drop benefits/tuition mentions, else record (rank, scope)."""
        seg, seg_start = _segment(text, start, end)
        if _BENEFITS.search(seg) or _in_benefits_section(text, start):
            return  # tuition-reimbursement perk, not a qualification
        if lang == "de" and _vocational_alternative_de(seg):
            return  # "Ausbildung oder Studium" — the academic degree isn't actually required
        if lang == "de" and _CURRENT_STUDENT_DE.search(seg):
            return  # "neben/nach Deinem Studium" — candidate's own ongoing studies, not a requirement
        if lang == "es" and _CURRENT_STUDENT_ES.search(seg):
            return  # "Estudiante universitario de...", "Matrícula en la universidad" — an ongoing
            # (not yet completed) program, e.g. an internship for enrolled students — not a
            # completed-degree requirement.
        if (
            lang == "es"
            and text[start:end].lower().startswith("ingenier")
            and _INGENIERIA_FIELD_DESC_ES.search(text[max(0, start - 20) : start])
        ):
            return  # "Empresa/perfil/sector/proyecto de ingeniería" — a field/domain descriptor,
            # not a degree requirement for this specific mention.
        mentions.append((rank, self._scope(text, seg, start - seg_start, start, lang)))

    @staticmethod
    def _scope(text: str, segment: str, pos: int, abs_start: int, lang: str = "en") -> bool | None:
        """Required(True) / preferred-only(False) / unstated(None) for one mention.

        ``pos`` is the mention's offset within ``segment``; ``abs_start`` its offset in ``text``.
        When a sentence carries BOTH cues ("BS required, MS preferred") the cue NEAREST the mention
        wins, so each degree gets its own scope. "or equivalent" counts as preferred-only (a
        degree-less candidate is not excluded) and, being adjacent to its degree, naturally outranks
        a trailing "required".

        Exception — the tight "<degree> or equivalent required" construction ("High school diploma
        or equivalent required", "... or equivalent (GED) required") IS a real requirement: the
        explicit "required" modifies the whole "degree-or-equivalent" phrase. This does NOT apply to
        "or equivalent EXPERIENCE required" (work experience substitutes for the degree -> still
        preferred-only), which the lookahead excludes.
        """
        if _EQUIV_REQUIRED.search(segment):
            return True
        checks: tuple[tuple[re.Pattern[str], bool], ...] = (
            (_OR_EQUIV, False),
            (_PREFERRED, False),
            (_REQUIRED, True),
        )
        if lang == "de":
            checks = (*checks, (_OR_EQUIV_DE, False))  # "oder vergleichbare Qualifikation" -> soft
        elif lang == "fr":
            checks = (*checks, (_OR_EQUIV_FR, False))  # "ou équivalent" -> soft
        elif lang == "es":
            checks = (*checks, (_OR_EQUIV_ES, False))  # "o equivalente" -> soft
        hits: list[tuple[int, bool]] = []
        for pat, verdict in checks:
            for m in pat.finditer(segment):
                hits.append((min(abs(m.start() - pos), abs(m.end() - pos)), verdict))
        if hits:
            return min(hits)[1]  # nearest cue wins; tie -> False (conservative)
        # No cue in the sentence: fall back to the governing section header.
        return _section_scope(text, abs_start)


register_extractor(DegreeExtractor())
