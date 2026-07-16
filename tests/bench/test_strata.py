from scripts.bench.strata import allocate


def test_floor_first_then_proportional_remainder():
    avail = {"greenhouse": 5000, "coveo": 40, "peopleadmin": 120}
    out = allocate(avail, total=1000, floor=100)
    assert out["coveo"] == 40  # floor capped at availability
    assert out["peopleadmin"] >= 100  # small provider gets its floor
    assert sum(out.values()) == 1000
    assert all(out[k] <= avail[k] for k in avail)


def test_total_capped_at_available():
    out = allocate({"a": 10, "b": 5}, total=1000, floor=100)
    assert out == {"a": 10, "b": 5}  # cannot draw more than exists


def test_floor_never_exceeds_total_budget():
    # Degenerate floor*strata > total: the floor pass must still respect the budget, not overshoot.
    out = allocate({"only": 7}, total=3, floor=10)
    assert sum(out.values()) == 3
    out2 = allocate({"a": 100, "b": 100, "c": 100}, total=50, floor=40)
    assert sum(out2.values()) == 50
    assert all(v >= 0 for v in out2.values())
