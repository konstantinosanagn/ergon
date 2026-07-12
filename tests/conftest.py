"""Shared pytest fixtures for ergon_tracker tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: hits real ATS APIs; skipped unless ERGON_LIVE_TESTS=1"
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    # Live tests make real network calls; skip them unless explicitly opted in. This lives in the
    # ROOT conftest (not a nested tests/live/conftest.py) so there is only one top-level `conftest`
    # module — a second one shadows this file's `load_fixture` and breaks suite-wide collection.
    if os.environ.get("ERGON_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live test — set ERGON_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def load_fixture(name: str) -> str:
    """Read a raw response fixture from tests/fixtures/<name>."""
    return (FIXTURES / name).read_text(encoding="utf-8")
