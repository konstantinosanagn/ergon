"""Keyword text handling, shared by every path that matches keywords against postings.

There were three rules, and they disagreed:

  index/query.py   ``[a-z0-9]+``          the FTS/SQL path
  ranking.py       ``[a-z0-9]+``          BM25 scoring
  models.py        ``.lower().split()``   the client-side live path

An ASCII-only class breaks both ends of the same query. ``Ingénieur`` shredded into
``["ing", "nieur"]``, neither of which is in the index — ``schema.sql`` declares
``tokenize="porter unicode61 remove_diacritics 2"``, so the stored term is ``ingenieur``. And a
CJK, Cyrillic or Greek query produced NO tokens at all, which fell through to a filter-only query
and returned the newest rows with no keyword constraint: arbitrary results rather than none.
107,924 active rows carry non-ASCII titles.

The FTS path does NOT use ``fold`` — see ``index/query.py``. FTS5 tokenizes the contents of a
quoted term itself, so passing the raw term through gets SQLite's exact behaviour (including the
porter stemmer) for free. Reimplementing it is a trap: a blanket NFKD + strip-combining-marks
turns ``エンジニア`` into ``エンシニア``, because ``ジ`` decomposes to ``シ`` plus a combining dakuten,
and it strips Greek accents that SQLite keeps. Verified against a real FTS5 table.

``fold`` therefore exists only for the in-Python paths (ranking, client-side filtering), and is
deliberately conservative: it removes a combining mark only when the base character is ASCII. That
folds ``é→e``, ``ü→u``, ``ñ→n`` without touching any script where the mark carries meaning.
"""

from __future__ import annotations

import re
import unicodedata

# Unicode letters + digits, minus underscore.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def fold(text: str) -> str:
    """Lowercase, and strip diacritics ONLY from ASCII base characters.

    Conservative on purpose: a blanket combining-mark strip corrupts scripts where the mark is
    part of the letter (Japanese dakuten, Greek tonos). Latin accents are the case that actually
    needs folding for keyword matching.
    """
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", text.lower()):
        if unicodedata.combining(ch):
            if out and out[-1].isascii():
                continue  # an accent on a Latin base -> drop it (café -> cafe)
            out.append(ch)  # anything else -> the mark is part of the letter, keep it
        else:
            out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def tokenize(text: str) -> list[str]:
    """Fold, then split into Unicode alphanumeric runs. For the in-Python paths only."""
    return _WORD.findall(fold(text))


def has_word_chars(text: str) -> bool:
    """True when ``text`` contains anything searchable at all.

    Distinguishes "the user typed no keywords" (no filter) from "the user typed only punctuation"
    (nothing can match) — the FTS path must not treat the second as the first.
    """
    return _WORD.search(text) is not None


def split_terms(text: str) -> list[str]:
    """Whitespace-split into terms carrying at least one word character.

    Terms are returned RAW, not folded: the FTS path quotes them and lets SQLite tokenize.
    """
    return [t for t in text.split() if has_word_chars(t)]
