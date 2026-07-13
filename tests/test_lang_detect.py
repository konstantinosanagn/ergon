"""Tests for the stdlib stopword-heuristic language detector (``extract/lang.py``)."""

from __future__ import annotations

from ergon_tracker.extract.lang import LANG_STOPWORDS, detect_language

_ENGLISH_JD = (
    "We are looking for a Senior Software Engineer to join our growing team. You will "
    "work closely with our product and design teams to build scalable systems. The ideal "
    "candidate has strong experience with distributed systems and a passion for mentoring "
    "others. We offer a competitive salary and a supportive, collaborative environment."
)

_GERMAN_JD = (
    "Wir suchen ab sofort eine Softwareentwicklerin (m/w/d) für unser Team in Berlin. "
    "Sie bringen mindestens fünf Jahre Berufserfahrung mit und arbeiten gerne im Team. "
    "Wir bieten Ihnen ein attraktives Gehalt sowie flexible Arbeitszeiten und freuen uns "
    "auf Ihre Bewerbung."
)


def test_detect_language_english_jd() -> None:
    assert detect_language(_ENGLISH_JD) == "en"


def test_detect_language_german_jd() -> None:
    assert detect_language(_GERMAN_JD) == "de"


def test_detect_language_empty_and_garbage_default_to_english() -> None:
    assert detect_language(None) == "en"
    assert detect_language("") == "en"
    assert detect_language("   ") == "en"
    assert detect_language("!!! 12345 ??? xyzxyz 000") == "en"


def test_lang_stopwords_has_en_and_de() -> None:
    # At minimum en/de per the spec; fr/es included so later language work is easy.
    assert "en" in LANG_STOPWORDS
    assert "de" in LANG_STOPWORDS
    assert "fr" in LANG_STOPWORDS
    assert "es" in LANG_STOPWORDS
    for lang, words in LANG_STOPWORDS.items():
        assert len(words) >= 25, f"{lang} stopword set too small ({len(words)})"


def test_detect_language_short_ambiguous_text_defaults_to_english() -> None:
    # A couple of stray German-ish tokens shouldn't be enough to flip the verdict — the
    # margin requirement protects against noise (company names, code fragments, ...).
    assert detect_language("Team Berlin GmbH") == "en"
