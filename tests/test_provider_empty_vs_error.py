"""A failed board fetch must never read as "this board is empty".

``fetch()`` returning ``[]`` is a *factual claim*: the board has zero postings. The freshness sweep
and the liveness pass both act on it — an empty id-set is a full departure — so a provider that
swallows a 429/5xx/timeout into ``[]`` is asserting every posting on that board disappeared.
Fourteen providers did exactly that.

The guard is behavioural, not textual: it drives every provider's real ``fetch()`` through
``board_live_raws`` (the shared primitive behind the membership diff) with a fetcher that fails on
every call, and asserts the answer is ``None`` — "could not determine" — rather than ``[]``.

NOTE on load_builtins: without it ``get_provider`` returns ``None`` and ``board_live_raws`` short-
circuits, so every provider "passes" and the test proves nothing. The first version of this probe
had exactly that defect and reported identical results before and after the fix.
"""

from __future__ import annotations

import httpx
import pytest

from ergon.index.freshness import board_live_raws
from ergon.providers.base import load_builtins

load_builtins()

# Providers whose fetch() previously swallowed a total failure into []. dayforce is excluded: it
# ignores the injected fetcher entirely and drives a real browser via playwright, so this probe
# cannot reach it offline (that bypass is itself worth fixing, separately).
_SWALLOWERS = [
    "brassring",
    "jazzhr",
    "jobdiva",
    "jobvite",
    "pageup",
    "paycom",
    "peopleadmin",
    "peopleclick",
    "peoplesoft",
    "personio",
    "taleo",
    "tesla",
    "zwayam",
]


class _DeadFetcher:
    """Every call fails at the transport layer. Nothing here says a board is empty."""

    def _boom(self, url: str = "https://example.test") -> Exception:
        return httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    async def get_text(self, url: str, **kw: object) -> str:
        raise self._boom(url)

    async def get_json(self, url: str, **kw: object) -> object:
        raise self._boom(url)

    async def request(self, method: str, url: str, **kw: object) -> object:
        raise self._boom(url)

    async def conditional_get(self, url: str, **kw: object) -> object:
        raise self._boom(url)


@pytest.mark.parametrize("source", _SWALLOWERS)
async def test_dead_board_is_undetermined_not_empty(source: str) -> None:
    """``None`` means "could not determine" and keeps the rows; ``[]`` expires them."""
    result = await board_live_raws(source, "acme", _DeadFetcher())
    assert result != [], (
        f"{source}: a totally failed fetch reported an EMPTY board, which the freshness/liveness "
        f"passes read as 'every posting departed' and expire the whole board"
    )
    assert result is None, f"{source}: expected None (undetermined), got {result!r}"


def test_the_probe_would_catch_a_regression() -> None:
    """Guard the guard: the registry must be loaded, or every case passes vacuously."""
    from ergon.providers.base import get_provider

    assert get_provider("jazzhr") is not None, (
        "load_builtins() did not run — board_live_raws would short-circuit to None for every "
        "source and this whole module would pass without testing anything"
    )
