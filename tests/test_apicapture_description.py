"""Guards for the in-list JD-capture harvest: the newly-mapped ``description`` dot-paths must
populate ``description_html`` via ``normalize`` (offline fixtures, no live network), and the
pre-existing description mappings must stay byte-for-byte unchanged (add-only invariant).

Context: 8 apicapture specs already returned a JD body in their captured list response but never
mapped it to ``description`` in ``fields``. Mapping the existing field is a free win — no new fetch.
This test pins each new mapping to a fixture record and asserts non-empty output.
"""

from __future__ import annotations

from typing import Any

from ergon_tracker.models import RawJob
from ergon_tracker.providers.apicapture import ApiCaptureProvider, _load_specs

# --- specs newly mapped in this harvest: token -> (description dot-path, marker JD in the fixture).
# The dot-path MUST equal the value now stored in apicapture.json; the fixture places the marker at
# exactly that path so a broken/renamed mapping fails loudly.
NEWLY_MAPPED: dict[str, str] = {
    "annapurnalabs": "description",
    "dataquad": "content.rendered",
    "decisionsix": "content.rendered",
    "infocons": "content.rendered",
    "inrika": "content.rendered",
    "sdhsystems": "content.rendered",
    "vertonsolutions": "content.rendered",
    "deshaw": "data.jobDescription.websiteDescription",
}

# The description mappings that existed BEFORE this harvest (captured from git HEAD). The harvest is
# strictly add-only: every one of these must still resolve to the same dot-path. ``federalsoftsystems``
# is intentionally an empty string (its captured list carries only a 33-char company boilerplate, no
# real JD) and must be left untouched.
PREEXISTING: dict[str, str] = {
    "amazon": "description_short",
    "ancile": "content.rendered",
    "applabsystems": "job_descplace",
    "apple": "jobSummary",
    "aruplaboratories": "BriefDescription",
    "bnpparibassecurities": "content.rendered",
    "camelotintegratedsolutions": "description",
    "caresoftglobal": "description",
    "compunnelsoftwaregroup": "Job_Summary",
    "dataedgeusa": "description",
    "dvgtechsolutions": "content.rendered",
    "epamsystems": "description",
    "experisus": "jobAdvertisementTeaser",
    "federalsoftsystems": "",
    "floridainternationaluniversity": "DESCRIPTION",
    "gandaramentalhealthcenter": "BriefDescription",
    "kastechsolutions": "content.rendered",
    "mckinsey": "whatYouWillDo",
    "mountsinaihospital": "description",
    "namitus": "content.rendered",
    "neoprism": "content.rendered",
    "quadranttechnologies": "description",
    "regentsofuniversityofcaliforniaatriverside": "positionInformation",
    "tekninjas": "content.rendered",
    "tiktokusds": "description",
    "uber": "description",
    "universityofpittsburghphysicians": "description",
    "ventois": "description.html",
    "vistaltech": "content.rendered",
}


def _nest(path: str, value: Any) -> dict[str, Any]:
    """Build a record dict placing ``value`` at a dot-path (``a.b.c`` -> {a:{b:{c:value}}})."""
    keys = path.split(".")
    out: dict[str, Any] = {}
    cur = out
    for k in keys[:-1]:
        cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value
    return out


def _record_for(spec: dict[str, Any], desc_path: str, marker: str) -> dict[str, Any]:
    """A minimal captured record for ``spec``: an id, a title, and the JD marker at ``desc_path``."""
    rec: dict[str, Any] = {}
    rec.update(_nest(desc_path, marker))
    # id + title so normalize builds a real JobPosting (dot-paths handled by _nest too).
    fid = spec["fields"].get("id") or "id"
    if fid not in rec:
        rec.update(_nest(fid, "JID-1"))
    ftitle = spec["fields"].get("title") or "title"
    # only add a title if it doesn't collide with the desc/id nesting
    top = ftitle.split(".")[0]
    if top not in rec:
        rec.update(_nest(ftitle, "Senior Widget Engineer"))
    return rec


def test_newly_mapped_specs_populate_description() -> None:
    specs = _load_specs()
    prov = ApiCaptureProvider()
    for token, expected_path in NEWLY_MAPPED.items():
        spec = specs.get(token)
        assert spec is not None, f"{token} missing from apicapture.json"
        got = spec["fields"].get("description")
        assert got == expected_path, f"{token}: description path drifted ({got!r} != {expected_path!r})"
        marker = f"<p>JD-BODY-{token}</p>"
        rec = _record_for(spec, expected_path, marker)
        raw = RawJob(
            source="apicapture",
            source_job_id="JID-1",
            company=spec.get("company") or token,
            token=token,
            url=None,
            payload={**rec, "_spec": spec["fields"]},
        )
        posting = prov.normalize(raw)
        assert posting.description_html == marker, f"{token}: description_html not populated"
        assert posting.description_text is None  # convention: all apicapture specs use _html


def test_preexisting_description_mappings_unchanged() -> None:
    """Add-only guard: the pre-harvest description mappings must be identical, and the file must now
    carry exactly the union of pre-existing + newly-mapped description keys."""
    specs = _load_specs()
    for token, path in PREEXISTING.items():
        spec = specs.get(token)
        assert spec is not None, f"{token} vanished from apicapture.json"
        assert spec["fields"].get("description") == path, f"{token}: pre-existing mapping changed"
    # federalsoftsystems stays intentionally empty (no real JD in its captured list).
    assert specs["federalsoftsystems"]["fields"]["description"] == ""

    have_desc = {t for t, s in specs.items() if "description" in s.get("fields", {})}
    assert have_desc == set(PREEXISTING) | set(NEWLY_MAPPED)
