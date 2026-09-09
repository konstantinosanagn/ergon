from ergon.index.scheduler import (
    BoardState,
    apply_outcome,
    assign_tier,
    due_boards,
    load_state,
    save_state,
)

TODAY = "2026-06-18"


def _s(**kw):
    return BoardState(provider=kw.pop("provider", "greenhouse"), token=kw.pop("token", "t"), **kw)


# --- tier policy ------------------------------------------------------------
def test_tier_hot_warm_cold_by_change_age():
    assert assign_tier(_s(last_changed="2026-06-16"), TODAY) == "hot"  # 2d
    assert assign_tier(_s(last_changed="2026-06-08"), TODAY) == "warm"  # 10d
    assert assign_tier(_s(last_changed="2026-05-01"), TODAY) == "cold"  # 48d


def test_tier_new_board_is_hot():
    assert assign_tier(_s(last_changed=None), TODAY) == "hot"


def test_tier_quarantine_on_errors_or_throttle():
    assert assign_tier(_s(last_changed="2026-06-17", consecutive_errors=3), TODAY) == "quarantine"
    assert assign_tier(_s(last_changed="2026-06-17", throttle_score=0.6), TODAY) == "quarantine"


# --- due selection ----------------------------------------------------------
def test_due_includes_hot_and_arrived_next_due_skips_future():
    states = [
        _s(token="hot", tier="hot"),
        _s(token="due", tier="cold", next_due="2026-06-18"),
        _s(token="future", tier="cold", next_due="2026-06-25"),
    ]
    keys = set(due_boards(states, TODAY))
    assert "greenhouse|hot" in keys and "greenhouse|due" in keys
    assert "greenhouse|future" not in keys


def test_quarantine_readmitted_after_cooldown():
    past = _s(token="q", tier="quarantine", next_due="2026-06-10")  # cooldown elapsed
    future = _s(token="q2", tier="quarantine", next_due="2026-07-01")
    keys = set(due_boards([past, future], TODAY))
    assert "greenhouse|q" in keys and "greenhouse|q2" not in keys


# --- back-pressure ----------------------------------------------------------
def test_outcome_change_resets_to_hot():
    s = _s(last_changed="2026-05-01", tier="cold", consecutive_unchanged=9)
    apply_outcome(s, today=TODAY, changed=True)
    assert s.tier == "hot" and s.last_changed == TODAY and s.consecutive_unchanged == 0


def test_repeated_unchanged_ages_board_and_lengthens_interval():
    # A board that keeps returning unchanged (e.g. a 304 every crawl) must age hot -> warm ->
    # cold so it is crawled progressively LESS often. This is what makes conditional requests
    # pay off: stable boards drop out of the daily crawl set.
    s = _s(last_changed="2026-06-01", consecutive_unchanged=0)  # last real change 17d ago
    apply_outcome(s, today=TODAY, changed=False)
    assert s.consecutive_unchanged == 1
    assert s.tier == "cold"  # >14d since last change
    # cold boards get a multi-day next_due (not crawled again tomorrow)
    assert s.next_due > TODAY
    assert s.last_changed == "2026-06-01"  # unchanged crawl never bumps last_changed


def test_outcome_throttle_drives_quarantine():
    s = _s(last_changed="2026-06-17")
    for _ in range(4):  # sustained 429s push EWMA past THROTTLE_MAX
        apply_outcome(s, today=TODAY, changed=False, http_429=10, requests=10)
    assert s.tier == "quarantine" and s.throttle_score >= 0.5


def test_outcome_errors_quarantine_and_recover():
    s = _s(last_changed="2026-06-17")
    for _ in range(3):
        apply_outcome(s, today=TODAY, changed=False, error=True)
    assert s.tier == "quarantine"
    apply_outcome(s, today=TODAY, changed=True)  # a good crawl recovers
    assert s.consecutive_errors == 0 and s.tier == "hot"


# --- persistence ------------------------------------------------------------
def test_state_round_trip(tmp_path):
    states = {"greenhouse|a": _s(token="a", tier="warm", throttle_score=0.1)}
    p = tmp_path / "state.json"
    save_state(states, p)
    loaded = load_state(p)
    assert loaded["greenhouse|a"].tier == "warm" and loaded["greenhouse|a"].throttle_score == 0.1


def test_load_missing_state_is_empty(tmp_path):
    assert load_state(tmp_path / "nope.json") == {}
