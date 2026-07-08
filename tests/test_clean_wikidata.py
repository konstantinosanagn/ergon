from __future__ import annotations

import pytest

cw = pytest.importorskip("scripts.clean_sector_wikidata")


def test_clean_drops_only_junk_industries() -> None:
    raw = {
        "goodco": {
            "sector": "Biotech/Pharma",
            "source": "wikidata",
            "wd_industry": "biotechnology",
        },
        "harper": {
            "sector": "Media/Entertainment",
            "source": "wikidata",
            "wd_industry": "pornography industry",
        },
        # a short 3-char slug with a legit industry is KEPT — length is not a drop signal
        # (it can't separate junk like `hud` from real companies like `2k`/`3m`/`abc`).
        "cba": {
            "sector": "Banking/Finance",
            "source": "wikidata",
            "wd_industry": "banking",
        },
    }
    cleaned, drops = cw.clean(raw)
    assert set(cleaned) == {"goodco", "cba"}  # only harper (junk industry) dropped
    assert cleaned["goodco"]["wd_industry"] == "biotechnology"  # full record preserved
    assert drops == {"junk_industry": 1}


def test_junk_industries_includes_pornography() -> None:
    assert "pornography industry" in cw.WD_JUNK_INDUSTRIES
