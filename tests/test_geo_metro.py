"""Metro-aware city matching: a city filter widens to the city's labelled variants (NYC boroughs,
"NYC", "SF") without pulling in same-named US states ("New York"/"Washington") or "X Park, MN"."""

from __future__ import annotations

from ergon_tracker.extract.geo import city_match_terms, city_matches


def test_new_york_widens_to_boroughs_and_synonyms():
    terms = set(city_match_terms("New York"))
    assert {"new york", "new york city", "nyc", "manhattan", "brooklyn", "queens"} <= terms
    # querying any borough resolves to the same group
    assert "new york" in city_match_terms("Brooklyn")


def test_city_matches_exact_variants():
    assert city_matches("New York", "New York City", "New York City, NY")
    assert city_matches("New York", "Brooklyn", "Brooklyn, NY")
    assert city_matches("New York", "NYC", "NYC")
    assert city_matches("New York", "New York ", "New York ")  # trailing space tolerated
    assert city_matches("San Francisco", "SF", "SF")


def test_city_matches_rejects_state_and_suburb_false_positives():
    # "Armonk, New York" is the STATE -> must NOT match a New York CITY filter.
    assert not city_matches("New York", "Armonk", "Armonk, New York")
    # "Brooklyn Park" is a Minneapolis suburb, not NYC's Brooklyn.
    assert not city_matches("New York", "Brooklyn Park", "Brooklyn Park, MN")
    assert not city_matches("New York", "Catskill", "Catskill, New York")


def test_non_metro_city_unchanged():
    assert city_match_terms("Austin") == ["austin"]
    assert city_matches("Austin", "Austin", "Austin, TX")
    assert not city_matches("Austin", "Houston", "Houston, TX")


def test_index_city_filter_is_metro_aware(tmp_path):
    from ergon_tracker.index.backend import SqliteIndexBackend
    from ergon_tracker.index.build import build_index
    from ergon_tracker.models import JobPosting, Location, SearchQuery

    def job(sid, city, raw):
        return JobPosting.create(
            source="greenhouse",
            source_job_id=sid,
            company="Co",
            title="Engineer",
            locations=[Location(raw=raw, city=city)],
        )

    p = tmp_path / "i.sqlite"
    build_index(
        [
            job("1", "New York", "New York, NY"),
            job("2", "New York City", "New York City, NY"),
            job("3", "Brooklyn", "Brooklyn, NY"),
            job("4", "Armonk", "Armonk, New York"),  # NY state, not NYC -> excluded
            job("5", "Austin", "Austin, TX"),
        ],
        p,
        build_id="b1",
    )
    got = {
        (j.locations[0].city if j.locations else None)
        for j in SqliteIndexBackend(p).search(SearchQuery(city="New York", limit=50))
    }
    # boroughs + city variants, NOT the NY-state town (Armonk) or Austin
    assert got == {"New York", "New York City", "Brooklyn"}
