from datetime import datetime, timezone

from ergon_tracker.index.mapping import from_row, to_row
from ergon_tracker.models import (
    JobLevel,
    JobPosting,
    Location,
    RemoteType,
    Salary,
    SalaryInterval,
)


def _job():
    return JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Stripe",
        title="Senior Backend Engineer",
        company_domain="stripe.com",
        description_text="Build payments. Rust and Go.",
        locations=[Location(city="Berlin", country="Germany", raw="Berlin, Germany")],
        remote=RemoteType.REMOTE,
        level=JobLevel.SENIOR,
        sector="Fintech",
        salary=Salary(
            min_amount=120000, max_amount=160000, currency="USD", interval=SalaryInterval.YEAR
        ),
        apply_url="https://x/1",
        posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        visa_sponsor=True,
        visa_last_filed="2026-03-31",
        sponsorship_offered=True,
        degree_min="bachelor",
        degree_required=True,
    )


def test_round_trip_preserves_indexed_fields():
    j = _job()
    j2 = from_row(to_row(j, build_id="b1"))
    assert j2.id == j.id and j2.company == "Stripe" and j2.title == j.title
    assert j2.level is JobLevel.SENIOR and j2.remote is RemoteType.REMOTE
    assert j2.sector == "Fintech" and j2.visa_sponsor is True and j2.sponsorship_offered is True
    assert j2.degree_min == "bachelor" and j2.degree_required is True
    assert j2.salary.min_amount == 120000 and j2.salary.currency == "USD"
    assert j2.locations[0].city == "Berlin" and j2.locations[0].country == "Germany"


def test_round_trip_preserves_degree_tri_state():
    # preferred-only (False) and unstated (None) must survive the 0/1/NULL SQLite encoding
    pref = _job().model_copy(update={"degree_min": "phd_md", "degree_required": False})
    j2 = from_row(to_row(pref, build_id="b1"))
    assert j2.degree_min == "phd_md" and j2.degree_required is False

    unknown = _job().model_copy(update={"degree_min": None, "degree_required": None})
    j3 = from_row(to_row(unknown, build_id="b1"))
    assert j3.degree_min is None and j3.degree_required is None


def test_to_row_sets_role_family_and_company_key():
    from ergon_tracker.dedup import normalize_company, normalize_title

    row = to_row(_job(), build_id="b1")
    assert row["company_key"] == normalize_company("Stripe")
    assert row["role_family"] == normalize_title("Senior Backend Engineer")
    assert row["snippet"].startswith("Build payments")


def test_to_row_sets_board_token():
    # Prereq for the liveness pass (index/liveness.py): board_token must survive into the built
    # row so a build-time pass can resolve which board to re-fetch without a registry lookup.
    job = _job().model_copy(update={"board_token": "acme-board-1"})
    row = to_row(job, build_id="b1")
    assert row["board_token"] == "acme-board-1"

    unset = to_row(_job(), build_id="b1")  # board_token defaults to None when never assigned
    assert unset["board_token"] is None


def test_content_hash_stable_and_change_sensitive():
    from ergon_tracker.index.mapping import content_hash
    from ergon_tracker.models import JobLevel, JobPosting, Salary

    base = JobPosting.create(
        source="greenhouse",
        source_job_id="1",
        company="Stripe",
        title="Backend Engineer",
        level=JobLevel.SENIOR,
    )
    same = JobPosting.create(
        source="lever",
        source_job_id="zzz",
        company="Stripe, Inc.",
        title="Backend Engineer",
        level=JobLevel.SENIOR,
    )
    diff = JobPosting.create(
        source="greenhouse", source_job_id="1", company="Stripe", title="Frontend Engineer"
    )
    relevel = base.model_copy(update={"level": JobLevel.MID})
    assert content_hash(base) == content_hash(same)  # same content, different source/id
    assert content_hash(base) != content_hash(diff)  # title changed
    assert content_hash(base) != content_hash(relevel)  # level is part of the identity
    withsal = base.model_copy(update={"salary": Salary(min_amount=100, max_amount=200)})
    assert content_hash(base) != content_hash(withsal)  # salary changed


def test_to_row_sets_enrich_hash():
    row = to_row(_job(), build_id="b1")
    assert row["enrich_hash"] and isinstance(row["enrich_hash"], str)


def test_enrich_hash_changes_when_jd_body_changes_even_if_content_hash_does_not():
    # The correctness-critical case: enrich_in_place extracts salary/yoe/degree/sector/
    # sponsorship FROM the JD body, so a rewritten body must invalidate the enrich cache even
    # when title/level/location/salary (content_hash's fields) are untouched.
    from ergon_tracker.index.mapping import content_hash, enrich_hash

    base = _job()
    rewritten = base.model_copy(
        update={"description_text": "Completely different responsibilities. Requires PhD."}
    )
    assert content_hash(base) == content_hash(rewritten)  # content_hash is blind to the body
    assert enrich_hash(base) != enrich_hash(rewritten)  # enrich_hash must NOT be blind to it


def test_enrich_hash_stable_under_whitespace_and_markup_only_changes():
    from ergon_tracker.index.mapping import enrich_hash

    base = _job()
    rewrapped = base.model_copy(
        update={"description_text": "  Build   payments.\n\nRust  and\tGo.  "}
    )
    assert enrich_hash(base) == enrich_hash(rewrapped)

    # description_html fallback (no description_text): tags-only difference, same visible words.
    html_a = base.model_copy(
        update={"description_text": None, "description_html": "<p>Build payments. Rust and Go.</p>"}
    )
    html_b = base.model_copy(
        update={
            "description_text": None,
            "description_html": "<div><p>Build payments.</p> <p>Rust and Go.</p></div>",
        }
    )
    assert enrich_hash(html_a) == enrich_hash(html_b)


def test_enrich_hash_equal_for_identical_postings_different_source_id():
    from ergon_tracker.index.mapping import enrich_hash

    base = _job()
    same = base.model_copy(update={"source": "lever", "source_job_id": "zzz"})
    assert enrich_hash(base) == enrich_hash(same)


def test_enrich_hash_falls_back_to_description_html_when_text_missing():
    from ergon_tracker.index.mapping import enrich_hash

    text_only = _job().model_copy(update={"description_html": None})
    html_only = _job().model_copy(
        update={"description_text": None, "description_html": "<p>Build payments. Rust and Go.</p>"}
    )
    assert enrich_hash(text_only) == enrich_hash(html_only)


def test_snippet_falls_back_to_stripped_description_html_when_text_missing():
    """html-only providers (jazzhr + ~15 others) capture the JD in description_html but leave
    description_text None; the snippet must fall back to stripped html so they aren't miscounted
    as no-JD (and needlessly queued for Tier-3 detail). Mirrors enrich's existing html fallback."""
    from ergon_tracker.index.mapping import to_row
    from ergon_tracker.models import JobPosting

    job = JobPosting.create(
        source="jazzhr", source_job_id="job_1", company="Acme", title="Engineer",
        description_html="<p>Build <b>great</b> things.</p>\n\n<ul><li>Python</li></ul>",
    )
    assert job.description_text is None
    row = to_row(job, build_id="b1")
    assert row["snippet"] == "Build great things. Python"  # tags stripped, whitespace collapsed


def test_snippet_prefers_description_text_over_html():
    from ergon_tracker.index.mapping import to_row
    from ergon_tracker.models import JobPosting

    job = JobPosting.create(
        source="greenhouse", source_job_id="1", company="Acme", title="Engineer",
        description_text="Plain text JD.", description_html="<p>ignored html</p>",
    )
    assert to_row(job, build_id="b1")["snippet"] == "Plain text JD."


def test_snippet_none_when_no_description_at_all():
    from ergon_tracker.index.mapping import to_row
    from ergon_tracker.models import JobPosting

    job = JobPosting.create(source="greenhouse", source_job_id="1", company="Acme", title="Engineer")
    assert to_row(job, build_id="b1")["snippet"] is None
