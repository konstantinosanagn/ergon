"""R1 durable guardrail: ONE writer per ``index-latest`` release asset.

Four daily GitHub Actions all publish to the single mutable ``index-latest`` release via
``gh release upload --clobber``. ``--clobber`` replaces an asset wholesale, so if two workflows both
upload the SAME asset name they race -- the later one silently discards the other's update. That is
the exact regression this change fixes: build-index.yml used to re-publish the drain's detail sidecar
(and the embed's vectors sidecar) that it merely downloaded, forcing build+drain+embed onto one shared
``concurrency`` group whose one-pending-eviction rule then STARVED the drain (JD coverage 85% -> 47%).

The fix is structural: every release asset now has EXACTLY ONE writer workflow, so ``--clobber`` can
never lose another writer's update and the shared group is no longer needed. This test parses the
publish steps of ALL FOUR workflows, extracts the set of release-asset names each one uploads, and
asserts those sets are PAIRWISE DISJOINT -- codifying "one writer per asset" so a future edit can't
silently reintroduce a shared writer.

Ownership after the fix:
* build-index.yml   -> core (index.sqlite.gz + manifest), slim, shards, jd, liveness, delta, plus
                       the build-metadata jsons (gates/board_state/history/cursors/...), fresh-rich,
                       crawl-progress, and the top-level manifest-set.
* drain-detail.yml  -> index-detail.sqlite.gz + manifest-detail.json
* embed-vectors.yml -> index-vectors.sqlite.gz + manifest-vectors.json
* freshness-sweep.yml -> index-freshness.sqlite.gz
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# The four workflows that write to the shared index-latest release.
RELEASE_WRITERS = [
    "build-index.yml",
    "drain-detail.yml",
    "embed-vectors.yml",
    "freshness-sweep.yml",
]

# A token counts as a release asset if it looks like one of the published file kinds. (The Actions
# `upload-artifact` step is NOT a `gh release upload`, so it is never scanned -- see _upload_steps.)
_ASSET_SUFFIXES = (".sqlite.gz", ".json", ".jsonl", ".md")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _load_run_steps(path: Path) -> list[str]:
    """Return the script of every ``run: |`` step in a workflow YAML.

    A dependency-free literal-block-scalar reader (PyYAML is not a project dependency): a ``run: |``
    key opens a block whose body is every following line indented STRICTLY MORE than the ``run:`` key
    (blank lines included), terminated by the next line at the key's indent or shallower. This keeps
    extraction scoped PER STEP -- so the ``actions/upload-artifact`` path list and the download steps
    (which are their own steps, without a ``gh release upload``) never leak into a publish step's set.
    """
    lines = path.read_text().splitlines()
    runs: list[str] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)run:\s*\|\s*$", lines[i])
        if not m:
            i += 1
            continue
        key_indent = len(m.group(1))
        body: list[str] = []
        i += 1
        while i < len(lines):
            ln = lines[i]
            if ln.strip() and _indent(ln) <= key_indent:
                break
            body.append(ln)
            i += 1
        runs.append("\n".join(body))
    return runs


def _decomment(run: str) -> list[str]:
    """Drop full-line ``#`` comments so a doc comment mentioning another asset can't pollute the
    extracted set. (Trailing inline comments in these publish scripts never carry ``dist/`` asset
    tokens, so line-leading stripping is sufficient and avoids mis-cutting quoted strings.)"""
    return [ln for ln in run.splitlines() if not ln.lstrip().startswith("#")]


def _is_asset(name: str) -> bool:
    return name.endswith(_ASSET_SUFFIXES)


def _assets_uploaded(run: str) -> set[str]:
    """Extract the release-asset basenames a single ``run`` script uploads.

    Sources, all resolved WITHIN the step (Actions steps don't share shell state, so every uploaded
    asset must be named in the same script):
      * ``for f in <names>; do`` bulk lists (bare basenames).
      * ``dist/<name>`` tokens on lines that build ``ASSETS=`` or invoke ``gh release upload/create``.
    """
    lines = _decomment(run)
    text = "\n".join(lines)
    assets: set[str] = set()

    # (a) `for VAR in a b c ...; do` bulk asset loops (may span backslash-continued lines).
    for m in re.finditer(r"\bfor\s+\w+\s+in\s+(.*?);\s*do\b", text, re.DOTALL):
        for tok in m.group(1).split():
            if _is_asset(tok):
                assets.add(tok)

    # (b) `dist/<name>` tokens on assignment / upload-command lines only (globs like shard-*.sqlite.gz
    #     are kept verbatim -- no other workflow uploads that literal, so it stays disjoint).
    for line in lines:
        if "ASSETS=" in line or "gh release upload" in line or "gh release create" in line:
            for tok in re.findall(r"dist/([\w.*-]+)", line):
                if _is_asset(tok):
                    assets.add(tok)
    return assets


def _uploaded_by(workflow: str) -> set[str]:
    path = WORKFLOWS / workflow
    assert path.exists(), f"missing workflow {path}"
    assets: set[str] = set()
    for run in _load_run_steps(path):
        if "gh release upload" in run or "gh release create" in run:
            assets |= _assets_uploaded(run)
    return assets


def test_every_release_asset_has_exactly_one_writer():
    """PAIRWISE DISJOINT: no release asset name is uploaded by two different workflows."""
    per_workflow = {wf: _uploaded_by(wf) for wf in RELEASE_WRITERS}

    # Sanity: parsing actually found each workflow's publish set (guards against a silent parser break
    # that would make everything vacuously disjoint).
    for wf, assets in per_workflow.items():
        assert assets, f"{wf}: parsed no release assets -- publish-step parser likely broke"

    for i, a in enumerate(RELEASE_WRITERS):
        for b in RELEASE_WRITERS[i + 1 :]:
            overlap = per_workflow[a] & per_workflow[b]
            assert not overlap, (
                f"{a} and {b} both upload {sorted(overlap)} -- two writers on one release asset "
                f"reintroduces the --clobber race R1 removed (one writer per asset)."
            )


def test_sidecars_are_owned_by_their_dedicated_workflow():
    """Lock in WHICH workflow owns each independently-published sidecar, and that build owns none of
    them (build only DOWNLOADS them: detail -> merged into `jobs`, vectors -> legacy rename)."""
    build = _uploaded_by("build-index.yml")
    drain = _uploaded_by("drain-detail.yml")
    embed = _uploaded_by("embed-vectors.yml")
    sweep = _uploaded_by("freshness-sweep.yml")

    assert {"index-detail.sqlite.gz", "manifest-detail.json"} <= drain
    assert {"index-vectors.sqlite.gz", "manifest-vectors.json"} <= embed
    assert "index-freshness.sqlite.gz" in sweep

    # Build must NOT publish any of the independently-owned sidecars (the R1 regression fix).
    for foreign in (
        "index-detail.sqlite.gz",
        "manifest-detail.json",
        "index-vectors.sqlite.gz",
        "manifest-vectors.json",
        "index-freshness.sqlite.gz",
    ):
        assert foreign not in build, f"build-index.yml must not upload {foreign} (single-writer)"

    # Build DOES own the core + its first-class sidecars (spot-check a few so a parser regression that
    # under-reads build's set is caught).
    assert {"index.sqlite.gz", "manifest.json", "manifest-set.json"} <= build
