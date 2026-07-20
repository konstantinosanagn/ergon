"""Offline tests for the minimum-degree extractor (gazetteer, scope, FP guards, real JDs)."""

from __future__ import annotations

import pytest

from conftest import load_fixture
from ergon_tracker.extract import get_extractor
from ergon_tracker.extract.base import ExtractInput
from ergon_tracker.extract.degree import DegreeExtractor
from ergon_tracker.models import DEGREE_LEVELS, DEGREE_ORDER


def _degree(description: str | None) -> tuple[str | None, bool | None]:
    return DegreeExtractor().extract(
        ExtractInput(title="Software Engineer", description_text=description)
    )


# --- gazetteer: every level, full names + abbreviations ----------------------


@pytest.mark.parametrize(
    "text,level",
    [
        # high school
        ("High school diploma required.", "highschool"),
        ("High school degree or GED.", "highschool"),
        # associate
        ("Associate's degree in accounting.", "associate"),
        ("Associate degree from a technical college.", "associate"),
        # bachelor
        ("Bachelor's degree in Computer Science.", "bachelor"),
        ("Bachelors degree needed.", "bachelor"),
        ("Bachelor of Science in Biology.", "bachelor"),
        ("B.S. in Mechanical Engineering.", "bachelor"),
        ("B.A. or related field.", "bachelor"),
        ("BSc in Physics.", "bachelor"),
        ("BS in Computer Science.", "bachelor"),
        ("Undergraduate degree from an accredited university.", "bachelor"),
        ("A 4-year degree is expected.", "bachelor"),
        ("Four year degree in a technical discipline.", "bachelor"),
        # master
        ("Master's degree in Statistics.", "master"),
        ("Master of Science in Data Science.", "master"),
        ("M.S. in Computer Science.", "master"),
        ("MS in Electrical Engineering.", "master"),
        ("MSc in Machine Learning.", "master"),
        ("An MBA is expected.", "master"),
        ("Advanced degree in a quantitative field.", "master"),
        ("Graduate degree in Economics.", "master"),
        # phd_md
        ("PhD in Chemistry.", "phd_md"),
        ("Ph.D. in Biology.", "phd_md"),
        ("Doctorate in Neuroscience.", "phd_md"),
        ("Doctoral degree in Physics.", "phd_md"),
        ("M.D. from an accredited medical school.", "phd_md"),
        ("PharmD licensure.", "phd_md"),
        ("DVM from an AVMA-accredited program.", "phd_md"),
        ("J.D. from an ABA-accredited law school.", "phd_md"),
        ("Juris Doctor with bar admission.", "phd_md"),
    ],
)
def test_gazetteer_levels(text: str, level: str) -> None:
    assert _degree(text)[0] == level


# --- slash alternations / multiple degrees -> MINIMUM level ------------------


@pytest.mark.parametrize(
    "text,level",
    [
        ("BS/MS in Computer Science.", "bachelor"),
        ("MS/PhD in a related field.", "master"),
        ("BS, MS or PhD in Engineering.", "bachelor"),
        ("PhD or equivalent, or BS with 5+ years of experience.", "bachelor"),
        (
            "Master's degree required; high school diploma with 10 years also considered.",
            "highschool",
        ),
    ],
)
def test_minimum_of_multiple_degrees(text: str, level: str) -> None:
    assert _degree(text)[0] == level


# --- new gazetteer forms: bare/plural masters, or-list associate/diploma, field/grad ----------


@pytest.mark.parametrize(
    "text,level",
    [
        # bare plural "Masters" (no apostrophe) + degree-context follower
        ("A Masters degree is expected.", "master"),
        ("Masters or PhD in a related field.", "master"),
        ("A Masters with 6+ years of relevant experience.", "master"),
        # singular/plural Master(s) as an or/slash list-arm beside another degree
        ("Master or Ph.D. in Physics.", "master"),
        ("Ph.D. / Masters in Engineering.", "master"),
        # or-list where a lower alt shares the trailing "degree" -> the associate floor is kept
        ("Diploma, Associate's, or Bachelor's degree in Architecture.", "associate"),
        ("Diploma or Bachelor's Degree in Computer Science.", "associate"),
        ("Associate's or Bachelor's Degree in IT.", "associate"),
        # field / academic degree, college graduate
        ("Engineering degree in a relevant discipline.", "bachelor"),
        ("An academic degree in a numerate subject.", "bachelor"),
        ("A recent college graduate is welcome to apply.", "bachelor"),
        ("University graduate with strong communication skills.", "bachelor"),
    ],
)
def test_new_gazetteer_forms(text: str, level: str) -> None:
    assert _degree(text)[0] == level


@pytest.mark.parametrize(
    "text",
    [
        # FP guards: the new bare/list forms must NOT fire outside a degree context
        "Certified Scrum Masters lead each squad.",  # plural title, no degree follower
        "We support master data management pipelines.",  # "master data", not a degree
        "Associate Engineer on the platform team.",  # job-title seniority, not "associate's degree"
        "The Associate or Principal will own delivery.",  # seniority levels in an or-list
    ],
)
def test_new_gazetteer_forms_fp_guards(text: str) -> None:
    assert _degree(text) == (None, None)


# --- scope: required / preferred / or-equivalent / unstated ------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Bachelor's degree in CS required.", ("bachelor", True)),
        ("Must have a Bachelor's degree.", ("bachelor", True)),
        ("Minimum education: Bachelor's degree.", ("bachelor", True)),
        ("Master's degree preferred.", ("master", False)),
        ("MBA a plus.", ("master", False)),
        ("PhD nice to have.", ("phd_md", False)),
        ("Ideally you hold a Master's degree.", ("master", False)),
        # "strongly preferred" is STILL preferred-only (the William Blair semantics)
        ("M.D. or Ph.D. in biology strongly preferred.", ("phd_md", False)),
        # "or equivalent" downgrades: a degree-less candidate is not excluded
        ("Bachelor's degree or equivalent experience.", ("bachelor", False)),
        ("Bachelor's degree or equivalent experience required.", ("bachelor", False)),
        ("4-year degree or equivalent practical experience.", ("bachelor", False)),
        # both cues in one sentence: the cue nearest each mention wins; min level's scope reported
        ("BS required, MS preferred.", ("bachelor", True)),
        # no cue, no governing section header -> defaults to required (a degree stated with no
        # qualifier reads as a requirement; degree_required is advisory)
        ("You will apply your Master's degree daily.", ("master", True)),
    ],
)
def test_scope_detection(text: str, expected: tuple[str, bool | None]) -> None:
    assert _degree(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # OR_EQUIV additions -> preferred-only (a degree-less candidate is not excluded)
        ("Bachelor's degree or an equivalent qualification.", ("bachelor", False)),  # article gap
        ("Bachelor's degree or equivalent practical experience.", ("bachelor", False)),  # adjective
        ("Bachelor's degree or professional experience in lieu of a degree.", ("bachelor", False)),
        ("Bachelor's degree or a combination of education and experience.", ("bachelor", False)),
        ("Bachelor's degree or 10 years of related experience.", ("bachelor", False)),
        # EQUIV_REQUIRED article mirror stays a real requirement
        ("High school diploma or an equivalent required.", ("highschool", True)),
        # PREFERRED additions -> preferred-only
        ("Master's degree preferred but not mandatory.", ("master", False)),
        ("A Bachelor's degree would be an advantage.", ("bachelor", False)),
        ("A Master's degree is beneficial for this role.", ("master", False)),
    ],
)
def test_new_scope_cues(text: str, expected: tuple[str, bool | None]) -> None:
    assert _degree(text) == expected


def test_section_header_context_defaults_to_required() -> None:
    text = "Qualifications:\nBachelor's degree in Engineering.\nStrong communication skills."
    assert _degree(text) == ("bachelor", True)
    text = "Minimum Qualifications\n- Bachelor's degree in CS or related field"
    assert _degree(text) == ("bachelor", True)


def test_preferred_section_header_wins_over_its_own_qualifications_word() -> None:
    # "Preferred Qualifications" contains the word "qualifications" — it must still read as
    # a preferred section, not a required one.
    text = "Preferred Qualifications:\nMaster's degree in a related field."
    assert _degree(text) == ("master", False)


def test_required_then_preferred_sections_take_min_level_and_its_scope() -> None:
    text = (
        "Minimum Qualifications:\nBachelor's degree in CS.\n\n"
        "Preferred Qualifications:\nMaster's degree in CS."
    )
    assert _degree(text) == ("bachelor", True)


# --- false-positive guards ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "A high degree of autonomy and ownership.",
        "You bring a high degree of professionalism.",
        "360 degree feedback culture.",
        "Operate the oven at 350 degrees.",
        "It can reach 100 degrees Fahrenheit in summer.",
        "A large degree of independence in this role.",
        "Must be 18 years of age or older.",
        "Experience with SQL Server 2019 and MS SQL administration.",
        "Proficient in MS Office and MS Excel.",
        "Located in Boston, MA or remote.",  # MA = Massachusetts, not an M.A.
        "Certified Scrum Master required.",  # Master-the-title, not master's-the-degree
        "Business Analyst supporting our BA practice leadership team meetings weekly.",
        "Tuition reimbursement toward your degree.",
        "We offer education assistance for any degree program.",
        "Benefits include tuition support toward a master's degree.",
        "",
    ],
)
def test_false_positives_return_none(text: str) -> None:
    assert _degree(text) == (None, None)


def test_empty_description_returns_none() -> None:
    assert _degree(None) == (None, None)


# --- ladder consistency + registry wiring -------------------------------------


def test_degree_ladder_is_canonical() -> None:
    assert DEGREE_LEVELS == ("highschool", "associate", "bachelor", "master", "phd_md")
    assert DEGREE_ORDER["highschool"] < DEGREE_ORDER["associate"] < DEGREE_ORDER["bachelor"]
    assert DEGREE_ORDER["bachelor"] < DEGREE_ORDER["master"] < DEGREE_ORDER["phd_md"]


def test_registered_under_name() -> None:
    extractor = get_extractor("degree")
    assert extractor is not None
    assert extractor.name == "degree"
    out = extractor.extract(ExtractInput(title="x", description_text="Bachelor's degree required."))
    assert out == ("bachelor", True)


# --- REAL job descriptions, end-to-end through enrich_in_place ----------------


def _enriched(fixture: str, title: str):
    from ergon_tracker.enrich import enrich_in_place
    from ergon_tracker.models import JobPosting

    job = JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Test Co",
        title=title,
        description_text=load_fixture(fixture),
    )
    return enrich_in_place(job)


def test_william_blair_equity_research_real_jd() -> None:
    # The motivating case: "M.D. or Ph.D. in biology or chemistry (or related life sciences
    # field) strongly preferred. Minimum of 1-2 years related work or internship experience."
    # A max_degree="bachelor" new-grad search must be able to exclude this posting.
    job = _enriched("jd_williamblair_equity_research.txt", "Equity Research Associate - BioTech")
    assert job.degree_min == "phd_md"
    assert job.degree_required is False  # "strongly preferred" is still preferred-only
    assert (job.years_experience_min, job.years_experience_max) == (1, 2)


def test_btig_equity_research_real_jd() -> None:
    # "Master's degree in chemical engineering preferred ..." + "1–3 years of experience in
    # equity research ..." + "BTIG does not offer sponsorship for work visas of any type".
    job = _enriched("jd_btig_chemical_analyst.txt", "Equity Research Associate, Biotechnology")
    assert job.degree_min == "master"
    assert job.degree_required is False
    assert (job.years_experience_min, job.years_experience_max) == (1, 3)
    assert job.sponsorship_offered is False


def test_enrich_never_overwrites_provider_degree() -> None:
    from ergon_tracker.enrich import enrich_in_place
    from ergon_tracker.models import JobPosting

    job = JobPosting.create(
        source="s",
        source_job_id="1",
        company="Acme",
        title="Engineer",
        description_text="PhD required.",
        degree_min="bachelor",
        degree_required=True,
    )
    enrich_in_place(job)
    assert job.degree_min == "bachelor" and job.degree_required is True


# --- the max_degree filter semantics (SearchQuery.matches) --------------------


def _job(**kw: object):
    from ergon_tracker.models import JobPosting

    base = {"source": "s", "source_job_id": "1", "company": "Acme", "title": "Engineer", **kw}
    return JobPosting.create(**base)  # type: ignore[arg-type]


def test_max_degree_filter_excludes_even_preferred_advanced_degrees() -> None:
    from ergon_tracker.models import SearchQuery

    william_blair_like = _job(degree_min="phd_md", degree_required=False)
    bachelor_job = _job(degree_min="bachelor", degree_required=True)
    unspecified = _job()

    q = SearchQuery(max_degree="bachelor")
    assert not q.matches(william_blair_like)  # preferred-only STILL excluded above the ceiling
    assert q.matches(bachelor_job)
    assert q.matches(unspecified)  # unknown kept by default

    strict = SearchQuery(max_degree="bachelor", include_unknown_degree=False)
    assert not strict.matches(unspecified)

    assert SearchQuery(max_degree="phd_md").matches(william_blair_like)  # ceiling high enough
    assert SearchQuery(max_degree="master").matches(_job(degree_min="highschool"))
    assert not SearchQuery(max_degree="highschool").matches(_job(degree_min="associate"))


def test_max_degree_rejects_invalid_value() -> None:
    import pydantic

    from ergon_tracker.models import SearchQuery

    with pytest.raises(pydantic.ValidationError):
        SearchQuery(max_degree="postdoc")  # type: ignore[arg-type]
