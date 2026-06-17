"""Seed registry integrity + resolver coverage (offline). Guards against a poisoned registry."""

from __future__ import annotations

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
