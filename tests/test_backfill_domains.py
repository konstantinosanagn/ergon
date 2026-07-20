"""Tests for scripts/backfill_domains.py — the add-only, idempotent seed domain backfill.

Every test runs the real ``backfill`` against a throwaway temp seed + temp sqlite index, so the
committed seed.json is never touched. Guards the invariants that make the backfill safe to re-run
for free as the index grows: add-only (never overwrite curated domains), idempotent (byte-stable),
ATS-vendor-host exclusion, deterministic collision resolution, and a clean format round-trip.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

spec = importlib.util.spec_from_file_location(
    "backfill_domains", ROOT / "scripts" / "backfill_domains.py"
)
assert spec and spec.loader
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)


# --- fixtures / helpers ------------------------------------------------------------------------
def make_index(path: Path, *, jobs: list[tuple[str, str]], companies: list[tuple[str, int]]) -> None:
    """Write a minimal index sqlite: ``jobs(company_key, company_domain)`` and
    ``companies(company_key, open_roles)`` — the only columns the backfill reads."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (company_key TEXT, company_domain TEXT)")
    con.execute("CREATE TABLE companies (company_key TEXT PRIMARY KEY, open_roles INTEGER)")
    con.executemany("INSERT INTO jobs VALUES (?, ?)", jobs)
    con.executemany("INSERT INTO companies VALUES (?, ?)", companies)
    con.commit()
    con.close()


def make_seed(path: Path, companies: dict[str, dict]) -> None:
    seed = {"_meta": {"version": 2, "updated": "2020-01-01"}, "companies": companies}
    path.write_text(json.dumps(seed, indent=1, ensure_ascii=False) + "\n")


def run(monkeypatch, seed_path: Path, index_path: Path, *, dry_run: bool = False) -> dict:
    """Point the backfill at temp files and run it (no real lock, no real seed)."""
    monkeypatch.setattr(bf, "SEED", seed_path)
    monkeypatch.setattr(bf, "seed_lock", contextlib.nullcontext)
    return bf.backfill(index_path, dry_run=dry_run)


@pytest.fixture()
def empty_index(tmp_path: Path) -> Path:
    p = tmp_path / "index.sqlite"
    make_index(p, jobs=[], companies=[])
    return p


# --- Source D unit tests -----------------------------------------------------------------------
def test_ats_vendor_hosts_excluded() -> None:
    # ATS backend hosts are the board plumbing, never the employer domain.
    assert bf.domain_from_token("x.icims.com") is None
    assert bf.domain_from_token("y.taleo.net|ex|101") is None
    assert bf.domain_from_token("foo.oraclecloud.com|CX_1") is None
    assert bf.domain_from_token("dollargeneral.jibeapply.com") is None
    assert bf.domain_from_token("accocareers.jobs.hr.cloud.sap|*|ACCO") is None
    # a truncated careers-prefix host with no real registrable domain
    assert bf.domain_from_token("careers.abb") is None


def test_real_company_hosts_reduced_to_apex() -> None:
    assert bf.domain_from_token("careers.ey.com|ey") == "ey.com"
    assert bf.domain_from_token("jobs.netapp.com|netapp") == "netapp.com"
    assert bf.domain_from_token("careers.acme.co.uk|x") == "acme.co.uk"  # ccTLD-aware apex
    assert bf.domain_from_token("candidate.fyi") == "candidate.fyi"  # company IS the domain
    assert bf.domain_from_token("https://www.citadel.com/x.xml") == "citadel.com"
    assert bf.domain_from_token("schemaorg:https://acme.com/sitemap.xml") == "acme.com"
    # a pure slug token embeds no host
    assert bf.domain_from_token("stripe") is None
    assert bf.domain_from_token("wmeimg|wd1|external") is None


# --- backfill behaviour ------------------------------------------------------------------------
def test_add_only_never_overwrites_even_if_index_disagrees(tmp_path, monkeypatch) -> None:
    seed = tmp_path / "seed.json"
    make_seed(seed, {"acme": {"ats": "lever", "token": "acme.com", "domain": "curated.com"}})
    # index insists on a *different* domain for the same normalized company
    idx = tmp_path / "index.sqlite"
    make_index(idx, jobs=[("acme", "other.com")], companies=[("acme", 99)])

    stats = run(monkeypatch, seed, idx)
    assert stats["added"] == 0
    data = json.loads(seed.read_text())
    assert data["companies"]["acme"]["domain"] == "curated.com"  # untouched


def test_source_d_fills_from_token_host(tmp_path, monkeypatch, empty_index) -> None:
    seed = tmp_path / "seed.json"
    make_seed(
        seed,
        {
            "netapp": {"ats": "successfactors", "token": "jobs.netapp.com|netapp", "domain": None},
            "icimsco": {"ats": "icims", "token": "foo.icims.com", "domain": None},
            "slugonly": {"ats": "greenhouse", "token": "stripe", "domain": None},
        },
    )
    stats = run(monkeypatch, seed, empty_index)
    data = json.loads(seed.read_text())["companies"]
    assert stats["added"] == 1
    assert stats["by_source_D"] == 1
    assert data["netapp"]["domain"] == "netapp.com"
    assert data["icimsco"]["domain"] is None  # ATS host excluded
    assert data["slugonly"]["domain"] is None  # no host in token


def test_collision_keeps_max_open_roles_key(tmp_path, monkeypatch) -> None:
    seed = tmp_path / "seed.json"
    # Two DISTINCT normalized keys whose token host reduces to the SAME new domain.
    make_seed(
        seed,
        {
            "acme": {"ats": "lever", "token": "shared.com", "domain": None},
            "acmelabs": {"ats": "lever", "token": "shared.com", "domain": None},
        },
    )
    idx = tmp_path / "index.sqlite"
    # no jobs rows -> Source A silent; open_roles differentiates the collision winner
    make_index(idx, jobs=[], companies=[("acme", 3), ("acmelabs", 20)])

    stats = run(monkeypatch, seed, idx)
    data = json.loads(seed.read_text())["companies"]
    assert stats["added"] == 1
    assert stats["skipped_collision"] == 1
    assert data["acmelabs"]["domain"] == "shared.com"  # more open_roles wins
    assert data["acme"]["domain"] is None


def test_collision_tiebreak_is_lexicographic(tmp_path, monkeypatch) -> None:
    seed = tmp_path / "seed.json"
    make_seed(
        seed,
        {
            "zzz-co": {"ats": "lever", "token": "shared.com", "domain": None},
            "aaa-co": {"ats": "lever", "token": "shared.com", "domain": None},
        },
    )
    idx = tmp_path / "index.sqlite"
    make_index(idx, jobs=[], companies=[("zzz-co", 5), ("aaa-co", 5)])  # equal roles

    run(monkeypatch, seed, idx)
    data = json.loads(seed.read_text())["companies"]
    assert data["aaa-co"]["domain"] == "shared.com"  # lexicographically smallest key wins
    assert data["zzz-co"]["domain"] is None


def test_idempotent_second_run_adds_zero_and_byte_identical(tmp_path, monkeypatch, empty_index) -> None:
    seed = tmp_path / "seed.json"
    make_seed(
        seed,
        {
            "netapp": {"ats": "successfactors", "token": "jobs.netapp.com|netapp", "domain": None},
            "ey": {"ats": "successfactors", "token": "careers.ey.com|ey", "domain": None},
        },
    )
    s1 = run(monkeypatch, seed, empty_index)
    assert s1["added"] == 2
    bytes_after_first = seed.read_bytes()

    s2 = run(monkeypatch, seed, empty_index)
    assert s2["added"] == 0
    assert seed.read_bytes() == bytes_after_first  # byte-identical


def test_dry_run_writes_nothing(tmp_path, monkeypatch, empty_index) -> None:
    seed = tmp_path / "seed.json"
    make_seed(seed, {"ey": {"ats": "successfactors", "token": "careers.ey.com|ey", "domain": None}})
    before = seed.read_bytes()
    stats = run(monkeypatch, seed, empty_index, dry_run=True)
    assert stats["added"] == 1  # would-add is reported
    assert seed.read_bytes() == before  # but nothing is written


def test_dirty_domain_rejected_by_shape_gate(tmp_path, monkeypatch) -> None:
    # A malformed index domain must never reach the seed.
    seed = tmp_path / "seed.json"
    make_seed(seed, {"acme": {"ats": "lever", "token": "acme", "domain": None}})
    idx = tmp_path / "index.sqlite"
    make_index(idx, jobs=[("acme", "INC.,not a domain")], companies=[("acme", 1)])
    stats = run(monkeypatch, seed, idx)
    assert stats["added"] == 0
    assert json.loads(seed.read_text())["companies"]["acme"]["domain"] is None


def test_format_round_trip_loads_and_resolves(tmp_path, monkeypatch, empty_index) -> None:
    """Written file re-parses, a SeedRegistry built over it loads, and a newly-added domain
    resolves via lookup_domain."""
    from ergon_tracker.registry import store as store_mod
    from ergon_tracker.registry.store import SeedRegistry

    seed = tmp_path / "seed.json"
    make_seed(
        seed, {"netapp": {"ats": "successfactors", "token": "jobs.netapp.com|netapp", "domain": None}}
    )
    run(monkeypatch, seed, empty_index)

    raw = json.loads(seed.read_text())  # re-parses cleanly
    assert raw["companies"]["netapp"]["domain"] == "netapp.com"

    # Point SeedRegistry at the temp seed and confirm the new domain resolves.
    monkeypatch.setattr(store_mod, "_load_seed", lambda: raw)
    reg = SeedRegistry()
    assert "netapp.com" in reg._by_domain
    res = reg.lookup_domain("netapp.com")
    assert res is not None and res.matched and res.ats == "successfactors"
    # a careers subdomain of the apex still resolves
    assert reg.lookup_domain("careers.netapp.com") is not None
