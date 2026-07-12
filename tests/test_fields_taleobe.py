"""Field-mapping regression tests for the Taleo Business Edition (TBE/CwsV2) provider.

Covers the location/employment_type/department div-classification bug found in inventory-D:
TBE card markup renders 1-3 ``<div>`` cells after the title, but their ORDER is tenant-specific
(not "div #1 is always location"). Tested at the parse level (``_parse_rows``), which is pure and
needs no network mocking, per the module's ``_ROW``/``_classify_divs`` seam.

Real (sampled) markup shapes exercised here, per inventory-D:
- Caltech (was already correct): divs = location, employment_type, department, in that order.
- NVR Inc (was mis-parsing): divs = department, location — no employment_type div, and location
  is NOT div #1, so the old "always take div #1" logic captured the department text as location.
"""

from __future__ import annotations

from ergon_tracker.models import EmploymentType
from ergon_tracker.providers.taleobe import _match_employment, _parse_rows

HOST = "phf.tbe.taleo.net/phf03"


def _card(rid: str, title: str, *divs: str) -> str:
    div_html = "".join(f'<div tabindex="0">{d}</div>' for d in divs)
    return (
        f'<h4 class="oracletaleocwsv2-head-title"><a href="https://{HOST}/ats/careers/v2/'
        f'viewRequisition?org=CALTECH&cws=37&rid={rid}" class="viewJobLink">{title}</a></h4>'
        f"{div_html}"
    )


def test_caltech_three_divs_location_employment_department_in_order() -> None:
    """Caltech renders 3 divs in the documented order: location, employment_type, department."""
    html = f"<html>{_card('37001', 'Administrative Assistant', 'Pasadena, CA', 'Fulltime Regular', 'Office of the General Counsel')}</html>"

    rows = _parse_rows(html)

    assert len(rows) == 1
    href, rid, title, location, employment_type_raw, department = rows[0]
    assert rid == "37001"
    assert title == "Administrative Assistant"
    assert location == "Pasadena, CA"
    assert employment_type_raw == "Fulltime Regular"
    assert department == "Office of the General Counsel"
    assert _match_employment(employment_type_raw) == EmploymentType.FULL_TIME


def test_nvr_two_divs_department_then_location_was_mistagged() -> None:
    """NVR Inc renders 2 divs as (department, location) — the reverse of the naive "div #1"
    assumption. The old code captured "Accounting / Finance" (department) as location; the fix
    must recognize "VA - Reston" as the location by shape, regardless of its position.
    """
    html = (
        f"<html>{_card('52001', 'Staff Accountant', 'Accounting / Finance', 'VA - Reston')}</html>"
    )

    rows = _parse_rows(html)

    assert len(rows) == 1
    href, rid, title, location, employment_type_raw, department = rows[0]
    assert rid == "52001"
    assert title == "Staff Accountant"
    # This is the regression: location must be the actual location div, not the first div.
    assert location == "VA - Reston"
    assert department == "Accounting / Finance"
    assert employment_type_raw is None


def test_sullcrom_blank_placeholder_div_then_location() -> None:
    """Sullivan & Cromwell renders a blank placeholder div, then the real location. The old code
    captured the empty string as location; only one div is non-blank so it must always win.
    """
    html = f"<html>{_card('38001', 'Associate', '', 'New York')}</html>"

    rows = _parse_rows(html)

    assert len(rows) == 1
    _, rid, title, location, employment_type_raw, department = rows[0]
    assert rid == "38001"
    assert location == "New York"
    assert employment_type_raw is None
    assert department is None


def test_single_div_still_treated_as_location() -> None:
    """The documented single-div markup (no employment_type/department cells at all) must keep
    working exactly as before.
    """
    html = f"<html>{_card('101', 'Research Scientist (Remote)', 'Remote - US')}</html>"

    rows = _parse_rows(html)

    assert len(rows) == 1
    _, rid, title, location, employment_type_raw, department = rows[0]
    assert location == "Remote - US"
    assert employment_type_raw is None
    assert department is None


def test_department_named_contract_administration_not_misread_as_employment_type() -> None:
    """A DEPARTMENT div whose text merely CONTAINS an employment-vocabulary word ("Contract") as
    a substring — e.g. "Contract Administration" — must NOT be classified as the employment_type
    div. Only a div whose ENTIRE trimmed text is composed of employment-vocabulary tokens (e.g.
    exactly "Contract") should match. This is the Stage-1 review finding: the old substring
    ``.search`` match stole department divs like this.
    """
    html = f"<html>{_card('39001', 'Paralegal', 'New York, NY', 'Contract Administration')}</html>"

    rows = _parse_rows(html)

    assert len(rows) == 1
    _, rid, title, location, employment_type_raw, department = rows[0]
    assert location == "New York, NY"
    assert employment_type_raw is None
    assert department == "Contract Administration"


def test_temp_staffing_department_not_misread_as_employment_type() -> None:
    """Same finding, different vocabulary word ("Temp") embedded in a department name."""
    html = f"<html>{_card('39002', 'Recruiter', 'Chicago, IL', 'Temp Staffing')}</html>"

    rows = _parse_rows(html)

    assert len(rows) == 1
    _, rid, title, location, employment_type_raw, department = rows[0]
    assert location == "Chicago, IL"
    assert employment_type_raw is None
    assert department == "Temp Staffing"


def test_whole_value_employment_match_still_recognizes_real_employment_divs() -> None:
    """The whole-value requirement must not regress real employment-type divs: a div that IS
    (only) an employment term — including tenant-real multi-token forms like "Fulltime Regular"
    — still matches.
    """
    assert _match_employment("Contract") == EmploymentType.CONTRACT
    assert _match_employment("Temporary") == EmploymentType.TEMPORARY
    assert _match_employment("Regular") == EmploymentType.FULL_TIME
    assert _match_employment("Fulltime Regular") == EmploymentType.FULL_TIME
    assert _match_employment("Part-Time") == EmploymentType.PART_TIME
    # But NOT when the vocabulary word is just a substring of a larger, non-employment phrase.
    assert _match_employment("Contract Administration") is None
    assert _match_employment("Temp Staffing") is None


def test_three_div_card_with_no_employment_match_drops_no_department() -> None:
    """A 3-div card where NONE of the divs match the (now whole-value) employment vocabulary: only
    location is claimed by shape, leaving 2 department-like divs. Neither may be silently dropped
    — both must land in ``department`` (Stage-1 review finding #2).
    """
    html = f"<html>{_card('39003', 'Facilities Coordinator', 'Pasadena, CA', 'Contract Administration', 'Facilities')}</html>"

    rows = _parse_rows(html)

    assert len(rows) == 1
    _, rid, title, location, employment_type_raw, department = rows[0]
    assert location == "Pasadena, CA"
    assert employment_type_raw is None
    assert department is not None
    assert "Contract Administration" in department
    assert "Facilities" in department


def test_multiple_cards_in_one_page_dont_bleed_into_each_other() -> None:
    """Ensure the divs-blob for one card doesn't swallow the next card's <h4>/divs."""
    html = (
        "<html>"
        + _card("1", "Job One", "Pasadena, CA", "Fulltime Regular", "Legal")
        + _card("2", "Job Two", "Accounting / Finance", "VA - Reston")
        + "</html>"
    )

    rows = _parse_rows(html)

    assert len(rows) == 2
    assert rows[0][1] == "1"
    assert rows[0][3] == "Pasadena, CA"
    assert rows[0][4] == "Fulltime Regular"
    assert rows[0][5] == "Legal"
    assert rows[1][1] == "2"
    assert rows[1][3] == "VA - Reston"
    assert rows[1][5] == "Accounting / Finance"
