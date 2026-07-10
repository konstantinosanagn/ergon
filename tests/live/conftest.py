import os

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits real ATS APIs; skipped unless ERGON_LIVE_TESTS=1")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("ERGON_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live test — set ERGON_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
