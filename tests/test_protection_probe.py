def test_deliberately_failing_probe() -> None:
    """Temporary: verifies branch protection blocks a red PR. Deleted immediately after."""
    assert False, "intentional failure to test the merge gate"
