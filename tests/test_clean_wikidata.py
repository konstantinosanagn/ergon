from __future__ import annotations

import pytest

cw = pytest.importorskip("scripts.clean_sector_wikidata")


def test_clean_drops_junk_and_short_slugs() -> None:
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
        "hud": {
            "sector": "Manufacturing/Industrial",
            "source": "wikidata",
            "wd_industry": "shipbuilding",
        },
        "cba": {
            "sector": "Banking/Finance",
            "source": "wikidata",
            "wd_industry": "banking",
        },  # 3-char slug
    }
    cleaned, drops = cw.clean(raw)
    assert set(cleaned) == {"goodco"}  # harper=junk, hud+cba=short-slug
    assert cleaned["goodco"]["wd_industry"] == "biotechnology"  # full record preserved
    assert drops == {"junk_industry": 1, "short_slug": 2}


def test_junk_industries_includes_pornography() -> None:
    assert "pornography industry" in cw.WD_JUNK_INDUSTRIES
    assert cw.SHORT_SLUG_MAX == 3
