"""Guardrail for the 2026-07-27 publish-regression fix: the OPT-IN production publish.

A validation dispatch of the (shelved, experimental) crawl-mapreduce.yml workflow published a
lower-coverage full-crawl index over a better one, regressing prod JD coverage 76%->40%. Root cause:
experimental/validation builds wrote the SAME production index-latest release as the daily build. The
fix makes writing prod an explicit OPT-IN, so a validation run can't touch it:

* crawl-mapreduce.yml (experimental) defaults ``publish`` to FALSE -> a dispatch is a dry run.
* build-index.yml (daily production) defaults ``publish`` to TRUE -> the daily cron is unchanged.
* the release-upload step in each is gated on that input.

These are dependency-free text assertions over the workflow YAML (PyYAML is not a project dependency,
matching tests/test_release_asset_ownership.py's reader), so a future edit can't silently flip a
default or drop a gate and re-open the incident.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _input_default(text: str, name: str) -> str | None:
    """The ``default:`` value of a ``workflow_dispatch`` input, or None if the input is absent.

    Matches the input key then the first ``default:`` beneath it (inputs are 2-space-nested under
    ``inputs:``; the default is the sibling line following the description)."""
    m = re.search(
        rf"^      {re.escape(name)}:\s*$.*?^        default:\s*\"?([^\"\n]*)\"?\s*$",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return m.group(1) if m else None


def test_crawl_mapreduce_defaults_to_no_publish():
    """The experimental workflow must default publish=false: a validation dispatch is a dry run."""
    text = (WORKFLOWS / "crawl-mapreduce.yml").read_text()
    assert _input_default(text, "publish") == "false", (
        "crawl-mapreduce.yml must default the `publish` input to false so a validation dispatch "
        "cannot overwrite the production index-latest release"
    )


def test_crawl_mapreduce_publish_step_is_opt_in():
    """The reduce's release-upload step must be gated on publish=='true' (explicit opt-in)."""
    text = (WORKFLOWS / "crawl-mapreduce.yml").read_text()
    # The publish step's `if:` must require the input to be exactly 'true'.
    assert re.search(r"if:\s*\$\{\{\s*github\.event\.inputs\.publish\s*==\s*'true'\s*\}\}", text), (
        "crawl-mapreduce.yml's publish step must be gated on inputs.publish == 'true'"
    )
    # And the reduce build must pass --no-publish on the default (non-opt-in) path.
    assert "--no-publish" in text, "crawl-mapreduce.yml must pass --no-publish on the dry-run path"


def test_build_index_defaults_to_publish():
    """The daily production workflow must default publish=true: the scheduled cron keeps publishing."""
    text = (WORKFLOWS / "build-index.yml").read_text()
    assert _input_default(text, "publish") == "true", (
        "build-index.yml must default the `publish` input to true so the daily production build is "
        "unchanged"
    )


def test_build_index_publish_gate_only_skips_on_explicit_false():
    """The daily publish gate must skip ONLY on an explicit publish=='false'. A scheduled cron leaves
    inputs.publish blank, and '' != 'false' is true -> the daily build publishes exactly as before
    (the byte-identical-daily guardrail)."""
    text = (WORKFLOWS / "build-index.yml").read_text()
    assert re.search(
        r"if:\s*\$\{\{\s*github\.event\.inputs\.publish\s*!=\s*'false'\s*\}\}", text
    ), (
        "build-index.yml's publish step must gate on inputs.publish != 'false' (blank cron => publish)"
    )
