import sqlite3

from ergon_tracker.index.detail import DetailRef, detail_sig, ensure_detail_schema


def test_schema_and_sig():
    con = sqlite3.connect(":memory:")
    ensure_detail_schema(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(job_detail)")}
    assert {"id", "sig", "fetched_at", "attempts", "snippet",
            "salary_min", "salary_max", "salary_currency", "salary_interval",
            "years_min", "years_max", "degree_min", "degree_required",
            "sponsorship_offered"} <= cols
    # sig is stable + independent of the (to-be-fetched) description
    s1 = detail_sig({"content_hash": "abc", "title": "Eng", "level": "senior"})
    s2 = detail_sig({"content_hash": "abc", "title": "Eng", "level": "senior"})
    assert s1 == s2 and isinstance(s1, str)
    assert detail_sig({"content_hash": "xyz"}) != s1

def test_detailref_from_row():
    ref = DetailRef.from_row({"id": "1", "source": "oracle", "board_token": "t",
                              "apply_url": "http://x", "listing_url": None, "content_hash": "h"})
    assert ref.id == "1" and ref.source == "oracle" and ref.apply_url == "http://x"
