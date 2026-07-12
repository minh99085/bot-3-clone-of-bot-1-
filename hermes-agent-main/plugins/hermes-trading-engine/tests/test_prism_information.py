"""Tests for PRISM Phase 2 — information completeness tracker + hour-timing FSM (PAPER ONLY)."""

from engine.pulse.prism.information import (
    FSMState,
    InformationTracker,
    ingest_book_imbalance,
    ingest_cex_lead,
    ingest_quant_fair,
    ingest_tv_latest,
    max_tier_for_state,
    state_from_seconds_since_open,
)

# The always-available standing feeds (anchor is computed every tick; CEX/book/quant follow fast).
_STANDING = ("chainlink_anchor", "cex_lead", "quant_fair")


def test_I_near_zero_at_start():
    t = InformationTracker()
    assert t.completeness(1000.0) == 0.0


def test_I_rises_as_tv_signals_arrive():
    t = InformationTracker()
    now = 1000.0
    i0 = t.completeness(now)
    t.observe("tv_15m", now, now)
    i1 = t.completeness(now)
    t.observe("tv_30m", now, now)
    i2 = t.completeness(now)
    assert i0 < i1 < i2


def test_15m_tv_only_about_055():
    """Standing feeds fresh + tv_15m only. I ~ 0.53 with this weight table (tolerance band)."""
    t = InformationTracker()
    now = 1000.0
    for s in (*_STANDING, "tv_15m"):
        t.observe(s, now, now)
    i = t.completeness(now)
    assert 0.48 <= i <= 0.60


def test_15m_30m_tv_fresh_ge_070():
    t = InformationTracker()
    now = 1000.0
    for s in (*_STANDING, "book_imbalance", "tv_15m", "tv_30m"):
        t.observe(s, now, now)
    assert t.completeness(now) >= 0.70


def test_freshness_decay_lowers_I():
    t = InformationTracker()
    now = 1000.0
    t.observe("cex_lead", now, now)          # half-life 45s
    fresh_I = t.completeness(now)
    stale_I = t.completeness(now + 180.0)    # 4 half-lives later
    assert stale_I < fresh_I


def test_missing_signals_shrink_as_observed():
    t = InformationTracker()
    now = 1000.0
    assert set(t.missing_signals(now)) == set(t.weights)
    t.observe("tv_15m", now, now)
    assert "tv_15m" not in t.missing_signals(now)


def test_fsm_transitions_at_boundaries():
    assert state_from_seconds_since_open(0) == FSMState.WATCHING
    assert state_from_seconds_since_open(179) == FSMState.WATCHING
    assert state_from_seconds_since_open(180) == FSMState.TIER1_READY
    assert state_from_seconds_since_open(719) == FSMState.TIER1_READY
    assert state_from_seconds_since_open(720) == FSMState.TIER2_CONFIRM
    assert state_from_seconds_since_open(2099) == FSMState.TIER2_CONFIRM
    assert state_from_seconds_since_open(2100) == FSMState.LATE_WINDOW
    assert state_from_seconds_since_open(2999) == FSMState.LATE_WINDOW
    assert state_from_seconds_since_open(3000) == FSMState.EXPIRED
    assert state_from_seconds_since_open(None) == FSMState.WATCHING


def test_max_tier_sniper_only_in_tier2_and_late():
    assert max_tier_for_state(FSMState.WATCHING) == "none"
    assert max_tier_for_state(FSMState.TIER1_READY) == "harvester"
    assert max_tier_for_state(FSMState.TIER2_CONFIRM) == "sniper"
    assert max_tier_for_state(FSMState.LATE_WINDOW) == "sniper"
    assert max_tier_for_state(FSMState.EXPIRED) == "none"


def test_sniper_eligible_requires_I_and_state():
    t = InformationTracker()
    now = 1000.0
    for s in (*_STANDING, "book_imbalance", "tv_15m", "tv_30m"):
        t.observe(s, now, now)               # I >= 0.70
    # right FSM state (TIER2_CONFIRM at 800s) -> eligible
    assert t.is_sniper_eligible(now, sso=800.0) is True
    # early hour (WATCHING) -> not eligible even with high I
    assert t.is_sniper_eligible(now, sso=100.0) is False
    # expired -> not eligible
    assert t.is_sniper_eligible(now, sso=3100.0) is False


def test_sniper_blocked_when_I_below_floor():
    t = InformationTracker()
    now = 1000.0
    t.observe("tv_15m", now, now)            # low I (~0.16)
    assert t.is_sniper_eligible(now, sso=800.0) is False


def test_expected_completeness_monotonic():
    t = InformationTracker()
    e0 = t.expected_completeness_at_minute(0)
    e15 = t.expected_completeness_at_minute(15)
    e55 = t.expected_completeness_at_minute(55)
    e1440 = t.expected_completeness_at_minute(1440)
    assert 0.0 < e0 < e15 < e55 < e1440
    assert abs(e1440 - 1.0) < 1e-9


def test_ingest_tv_latest_maps_keys():
    now = 2000.0
    snap = {
        "BTCUSD@15": {"direction": "UP", "strength": 0.8, "ts": now - 10},
        "BTCUSD@30": {"direction": "DOWN", "strength": 0.6, "ts": now - 20},
        "BTCUSD@45": {"direction": "UP", "strength": 0.7, "ts": now - 5},
        "BTCUSD@55": {"direction": "FLAT", "strength": 0.5, "ts": now - 5},   # ignored (retired)
        "ETHUSD@15": {"direction": "UP", "strength": 0.9, "ts": now - 3},     # wrong symbol
    }
    t = InformationTracker()
    obs = ingest_tv_latest(snap, "BTCUSD", now, tracker=t)
    names = {o.name: o for o in obs}
    assert set(names) == {"tv_15m", "tv_30m", "tv_45m"}
    assert names["tv_15m"].direction == 1
    assert names["tv_30m"].direction == -1
    assert names["tv_45m"].direction == 1
    assert "tv_15m" in t.received_at and "tv_30m" in t.received_at and "tv_45m" in t.received_at


def test_ingest_cex_book_quant_helpers():
    now = 3000.0
    t = InformationTracker()
    cex = ingest_cex_lead({"cex_p_up": 0.62, "ts": now}, now, tracker=t)
    assert cex is not None and cex.direction == 1 and cex.name == "cex_lead"
    book = ingest_book_imbalance(-0.4, 0.02, now, tracker=t)
    assert book is not None and book.direction == -1
    quant = ingest_quant_fair(0.58, 0.50, now, tracker=t)
    assert quant is not None and quant.direction == 1
    # neutral / missing inputs -> None
    assert ingest_cex_lead(None, now) is None
    assert ingest_book_imbalance(0.0, 0.01, now) is None
    assert ingest_quant_fair(None, None, now) is None


def test_to_report_shape():
    t = InformationTracker()
    now = 1000.0
    for s in (*_STANDING, "tv_15m"):
        t.observe(s, now, now)
    rep = t.to_report(now, sso=800.0)
    assert rep["enabled"] is True
    assert 0.0 <= rep["I"] <= 1.0
    assert rep["fsm_state"] == "tier2_confirm"
    assert rep["sniper_max_tier"] == "sniper"
    assert "missing" in rep and "freshness" in rep
