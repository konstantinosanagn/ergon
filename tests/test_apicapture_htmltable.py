"""Unit tests for apicapture's spec-driven HTML-table parser (offline, pure function).

Some body-shops expose their board as a server-rendered ``<table>`` instead of a JSON API; the
``html_table`` spec mode parses it. Each column is extracted by ``{"col": N}`` (cell text) or
``{"re": pattern}`` (regex over the row's raw HTML — for an id/url buried in an ``href``).
"""

from __future__ import annotations

from ergon_tracker.providers.apicapture import _parse_html_table

_HTML = """
<table>
  <tr><td>JOB ID</td><td>Posted Date</td><td>Job Title</td><td>Location</td></tr>
  <tr>
    <td>J1</td><td>06/19/2026</td>
    <td><a href="https://x.com/view_job?mode=apply&amp;id=101">Senior Engineer</a></td>
    <td>Tampa, FL</td>
  </tr>
  <tr>
    <td>J2</td><td>06/18/2026</td>
    <td><a href="https://x.com/view_job?mode=apply&amp;id=102">Data Analyst</a></td>
    <td>Austin, TX</td>
  </tr>
</table>
"""

_SPEC = {
    "html_table": {"row_css": "tr", "skip_rows": 1},
    "columns": {
        "id": {"re": r"[&;?]id=(\d+)"},
        "title": {"col": 2},
        "location": {"col": 3},
        "posted_at": {"col": 1},
        "url": {"re": r"(https://x\.com/view_job[^\"]+)"},
    },
}


def test_parses_rows_with_col_and_regex() -> None:
    recs = _parse_html_table(_HTML, _SPEC)
    assert len(recs) == 2
    assert recs[0] == {
        "id": "101",
        "title": "Senior Engineer",
        "location": "Tampa, FL",
        "posted_at": "06/19/2026",
        "url": "https://x.com/view_job?mode=apply&id=101",
    }
    assert recs[1]["id"] == "102"
    assert recs[1]["title"] == "Data Analyst"


def test_regex_extraction_unescapes_entities() -> None:
    # The href carries &amp;; the extracted url must be decoded to a usable &.
    recs = _parse_html_table(_HTML, _SPEC)
    assert recs[0]["url"] == "https://x.com/view_job?mode=apply&id=101"
    assert "&amp;" not in recs[0]["url"]


def test_skip_rows_drops_header() -> None:
    # With skip_rows=0 the header row is still emitted by the parser, but every job-content field
    # is the header label and the id regex finds nothing -> id is None (the fetch loop drops it).
    spec = {**_SPEC, "html_table": {"row_css": "tr", "skip_rows": 0}}
    recs = _parse_html_table(_HTML, spec)
    assert recs[0]["id"] is None  # header row: no id= in it
    assert len(recs) == 3


def test_row_with_no_fields_is_dropped() -> None:
    recs = _parse_html_table("<table><tr></tr><tr><td>only</td></tr></table>", _SPEC)
    # First <tr> has no cells and no regex hit -> dropped; second has a col-0 but our spec reads
    # col 1..3 / regex, none match -> also dropped.
    assert recs == []
