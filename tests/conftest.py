"""Shared pytest fixtures for ergon_tracker tests."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def load_fixture(name: str) -> str:
    """Read a raw response fixture from tests/fixtures/<name>."""
    return (FIXTURES / name).read_text(encoding="utf-8")
