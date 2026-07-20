"""Seed registry integrity + resolver coverage (offline). Guards against a poisoned registry."""

from __future__ import annotations

import re

import pytest

from ergon_tracker.registry.store import SeedRegistry

SUPPORTED_ATS = {
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "smartrecruiters",
    "workable",
    "recruitee",
    "personio",
    "bamboohr",
    "breezy",
    "teamtailor",
    "join",
    "rippling",
    "pinpoint",
    "eightfold",
    "successfactors",
    "oracle",
    "taleo",
    "taleobe",
    "icims",
    "avature",
    "applicantpro",
    "jazzhr",
    "jobvite",
    "phenom",
    "brassring",
    "schemaorg",
    "apicapture",
    "coveo",
    "peopleadmin",
    "peopleclick",
    "jobdiva",
    "ripplehire",
    "zwayam",
    "ceipal",
    "radancy",
    "pageup",
    "peoplesoft",
    "ukg",
    "adp",
    "dayforce",
    "paycom",
    "tesla",
    "paylocity",
    "usajobs",
    "dejobs",
    "themuse",
    "adzuna",
}


@pytest.fixture(scope="module")
def registry() -> SeedRegistry:
    return SeedRegistry()


def test_registry_is_substantial(registry: SeedRegistry) -> None:
    # We grew the seed well beyond the original 13.
    assert len(registry) >= 200


def test_every_entry_has_valid_shape(registry: SeedRegistry) -> None:
    bad: list[str] = []
    for key, entry in registry.all().items():
        if entry.get("ats") not in SUPPORTED_ATS:
            bad.append(f"{key}: bad ats {entry.get('ats')}")
        elif not entry.get("token"):
            bad.append(f"{key}: empty token")
    assert not bad, bad


_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")

# Pre-existing EDGAR-extraction artifacts of the shape ``INC.,acme.com`` (a legal-form prefix
# leaked into the domain). They predate the domain backfill and are frozen here as a known
# baseline: the backfill's shape gate can never ADD one, so this count must not grow.
_LEGACY_DIRTY_DOMAINS = 28


def test_all_domains_are_clean_shaped(registry: SeedRegistry) -> None:
    """Every stored domain must pass the resolver's shape gate — the *only* tolerated exception is
    the frozen set of legacy ``<legal-form>,domain`` EDGAR artifacts. Any OTHER malformation
    (a space, a missing TLD, an ATS host with junk) is a poisoned registry and fails here. This is
    the invariant the domain backfill must never violate: its pre-write gate rejects anything that
    doesn't match ``_DOMAIN_RE``, so a clean run can only ever keep this set clean or shrink it."""
    from ergon_tracker.registry.store import _normalize_domain

    non_clean: list[str] = []
    for key, entry in registry.all().items():
        domain = entry.get("domain")
        if domain is None:
            continue
        if not isinstance(domain, str) or not _DOMAIN_RE.match(_normalize_domain(domain)):
            non_clean.append(f"{key}: {domain!r}")
    # No new *kinds* of dirt: every non-clean value is the known legacy comma artifact.
    unexpected = [x for x in non_clean if "," not in x.split(": ", 1)[1]]
    assert not unexpected, f"unexpected dirty domains (not legacy comma artifacts): {unexpected}"
    # And the legacy baseline never grows (the backfill adds only clean domains).
    assert len(non_clean) <= _LEGACY_DIRTY_DOMAINS, non_clean


def test_workday_tokens_are_three_part_composite(registry: SeedRegistry) -> None:
    bad: list[str] = []
    for key, entry in registry.all().items():
        if entry["ats"] == "workday":
            parts = entry["token"].split("|")
            if len(parts) != 3 or not all(parts):
                bad.append(f"{key}: {entry['token']}")
    assert not bad, bad


def test_company_keys_are_unique_and_lowercase(registry: SeedRegistry) -> None:
    keys = list(registry.all().keys())
    assert len(keys) == len(set(keys))
    assert all(k == k.lower() for k in keys)


def test_resolver_resolves_known_seed_domains(registry: SeedRegistry) -> None:
    from ergon_tracker.registry.resolver import resolve

    # A sample of newly added companies should resolve via the seed by domain.
    samples = {
        "figma.com": "greenhouse",
        "adobe.com": "workday",
        "notion.so": "ashby",
        "crypto.com": "lever",
    }
    for domain, expected_ats in samples.items():
        res = resolve(domain)
        assert res.matched, f"{domain} did not resolve"
        assert res.ats == expected_ats, f"{domain} -> {res.ats}, expected {expected_ats}"
        assert res.token


def test_distribution_across_ats(registry: SeedRegistry) -> None:
    seen = {entry["ats"] for entry in registry.all().values()}
    # every ATS present in the registry must be a supported provider
    assert seen <= SUPPORTED_ATS
    # the four original ATS must all be represented
    assert seen >= {"greenhouse", "lever", "ashby", "workday"}


def test_demo_boards_excluded_and_purged():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "_br", pathlib.Path(__file__).parent.parent / "scripts" / "build_registry.py"
    )
    br = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(br)
    # Lever demo/training boards carry fake postings -> must be denied
    assert br.is_demo_board("lever", "leverdemo")
    assert br.is_demo_board("lever", "leverdemo-8")
    assert br.is_demo_board("lever", "leverdemo50000")
    assert not br.is_demo_board("lever", "stripe")  # real board
    assert not br.is_demo_board("greenhouse", "leverdemo")  # only lever's demo tokens
    # purge removes them from an existing registry dict
    companies = {
        "acme": {"ats": "lever", "token": "acme"},
        "lever-education": {"ats": "lever", "token": "leverdemo193"},
    }
    assert br.purge_demo_boards(companies) == 1
    assert "lever-education" not in companies and "acme" in companies


def test_live_seed_has_no_demo_boards():
    from ergon_tracker.registry.store import SeedRegistry

    r = SeedRegistry().all()
    bad = [
        k
        for k, e in r.items()
        if e.get("ats") == "lever" and str(e.get("token", "")).lower().startswith("leverdemo")
    ]
    assert bad == [], f"demo boards leaked into the registry: {bad}"
