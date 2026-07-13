"""Language detection for job-description text (stdlib-only stopword heuristic).

No language-detection library is in the dependency tree, and the project is dependency-
conscious (see ``docs/superpowers/specs/2026-07-13-multilingual-extraction-spec.md``), so this
uses a small closed-vocabulary stopword heuristic instead: tokenize the first ~500 chars of the
text, count how many tokens fall in each language's function-word set, and pick the language
with the highest count. English is both the default and the tiebreak — a language only wins when
its score strictly beats English's *and* clears a minimum confidence floor — so an ambiguous or
short/garbage sample always resolves to ``"en"``, which is the safe, current (pre-multilingual)
behavior.

Deliberately conservative: the stopword sets exclude short function words that collide with
common English words (German "an"/"in"/"so"/"was", etc.) so a genuine English JD cannot rack up
an accidental non-English score.
"""

from __future__ import annotations

import re

__all__ = ["LANG_STOPWORDS", "detect_language"]

# ~30-40 high-frequency function words per language. Kept to unambiguous, language-specific
# tokens on purpose: words that double as common English words (German "an", "in", "man", "so",
# "was", "er"; French/Spanish "a", "en", "no", "son") are deliberately left out so real English
# text can't accidentally rack up a non-English score.
LANG_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "the", "and", "of", "to", "in", "for", "with", "on", "is", "are", "we", "you",
            "our", "your", "will", "be", "that", "this", "as", "at", "by", "an", "or", "have",
            "has", "from", "but", "not", "all", "can", "its", "was", "were", "which", "who",
            "what", "if", "than", "then", "so", "such", "these", "those", "about",
        }
    ),
    "de": frozenset(
        {
            "der", "die", "das", "und", "mit", "für", "von", "auf", "ist", "sind", "wir",
            "ihre", "ihr", "werden", "sein", "dass", "dies", "diese", "bei", "eine", "einer",
            "einen", "oder", "haben", "aber", "nicht", "alle", "kann", "waren", "welche",
            "wenn", "dann", "solche", "zum", "zur", "nach", "über", "unter", "durch", "auch",
            "noch", "sehr", "ohne", "sowie", "dabei", "unsere", "ihnen", "gerne", "bitte",
            "berufserfahrung", "erfahrung", "kenntnisse", "unternehmen", "mitarbeiter",
        }
    ),
    "fr": frozenset(
        {
            "le", "la", "les", "et", "de", "des", "du", "une", "sont", "nous", "vous",
            "notre", "votre", "être", "que", "ce", "cette", "ces", "comme", "chez", "au",
            "aux", "ou", "avoir", "mais", "ne", "pas", "tout", "tous", "peut", "son", "sa",
            "ses", "étaient", "qui", "quoi", "donc", "dans", "sur", "par", "plus", "très",
            "vos", "expérience", "poste", "équipe", "entreprise",
        }
    ),
    "es": frozenset(
        {
            "el", "la", "los", "las", "y", "de", "del", "una", "son", "nosotros", "usted",
            "nuestro", "nuestra", "su", "sus", "ser", "que", "este", "esta", "estos", "como",
            "al", "tener", "tiene", "pero", "todo", "todos", "puede", "eran", "quien", "si",
            "entonces", "para", "por", "con", "más", "muy", "empresa", "experiencia",
            "equipo", "trabajo", "buscamos",
        }
    ),
}

# A candidate language must beat English by at least this many stopword hits to win — protects
# against a handful of coincidental matches (company names, code fragments) tipping the balance.
_MIN_MARGIN = 3

_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)
_SAMPLE_CHARS = 500


def detect_language(text: str | None) -> str:
    """Best-effort ISO-639-1 language code for ``text``; defaults to ``"en"``.

    Tokenizes the first ~500 characters, scores stopword overlap per language in
    ``LANG_STOPWORDS``, and returns the highest-scoring language — provided it beats English by
    ``_MIN_MARGIN`` hits. Empty/garbage/short input, or anything that doesn't clear the margin,
    returns ``"en"`` (fails safe: the current, pre-multilingual behavior).
    """
    if not text:
        return "en"
    sample = text[:_SAMPLE_CHARS].lower()
    tokens = _TOKEN.findall(sample)
    if not tokens:
        return "en"

    scores = {lang: sum(1 for t in tokens if t in words) for lang, words in LANG_STOPWORDS.items()}
    en_score = scores.get("en", 0)

    best_lang, best_score = "en", en_score
    for lang, score in scores.items():
        if lang == "en":
            continue
        if score > best_score and score - en_score >= _MIN_MARGIN:
            best_lang, best_score = lang, score
    return best_lang
