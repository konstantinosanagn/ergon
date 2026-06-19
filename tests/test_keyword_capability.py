"""Architectural contract: exactly which providers do server-side keyword search.

Locks the hybrid design documented on SearchQuery: only adzuna/coveo/smartrecruiters/usajobs/
workday pass `keywords` to their remote API; every other provider returns its whole board and
relies on the client-side `matches()` keyword filter. If someone adds/removes a server-side
keyword path, this test forces a conscious update (and a docstring update).
"""

from __future__ import annotations

import inspect

from ergon_tracker.providers.base import get_provider, load_builtins, provider_names

# Providers whose remote API supports keyword search (verified in their fetch()).
KEYWORD_CAPABLE = {"adzuna", "coveo", "smartrecruiters", "usajobs", "workday"}


def _fetch_uses_keywords(name: str) -> bool:
    provider = get_provider(name)
    src = inspect.getsource(type(provider).fetch)
    return ".keywords" in src


def test_exactly_the_expected_providers_do_server_side_keyword_search() -> None:
    load_builtins()
    capable = {n for n in provider_names() if _fetch_uses_keywords(n)}
    assert capable == KEYWORD_CAPABLE, (
        f"keyword-capable set changed: {capable} != {KEYWORD_CAPABLE}. "
        "Update SearchQuery's docstring + this contract if intentional."
    )


def test_capable_providers_are_registered() -> None:
    load_builtins()
    registered = set(provider_names())
    missing = KEYWORD_CAPABLE - registered
    assert not missing, f"keyword-capable providers not registered: {missing}"
