"""Tests for PRISM Phase 3 — optimal stopping engine (PAPER ONLY)."""

from engine.pulse.prism.stopping import (
    PRISMConfig,
    StoppingDecision,
    StoppingEngine,
    optimal_stopping_decide,
    payoff_shape,
)
from engine.pulse.prism.information import FSMState, state_from_seconds_since_open


def _engine():
    return StoppingEngine(PRISMConfig())


def test_payoff_shape_favors_sweet_spot():
    assert payoff_shape(0.50) == 1.0
    assert payoff_shape(0.47) == 1.0
    assert payoff_shape(0.55) == 1.0
    assert payoff_shape(0.30) < 1.0
    assert payoff_shape(0.80) < 1.0
    assert payoff_shape(None) == 0.5


def test_low_I_early_hour_waits():
    eng = _engine()
    r = eng.evaluate("w1", sso=100.0, ttc_s=3500.0, I=0.20, E=0.05, C=0.6, ask_price=0.50)
    assert r.decision == StoppingDecision.WAIT


def test_high_I_high_E_high_C_enters():
    eng = _engine()
    # sso=1000 -> TIER2_CONFIRM (sniper). R = 0.8*0.2*0.9 = 0.144 >= 0.12; I>=i_target -> V_wait<0.
    r = eng.evaluate("w2", sso=1000.0, ttc_s=1200.0, I=0.80, E=0.20, C=0.9, ask_price=0.50)
    assert r.decision == StoppingDecision.ENTER
    assert r.tier == "sniper"


def test_late_hour_skips():
    eng = _engine()
    r = eng.evaluate("w3", sso=3100.0, ttc_s=200.0, I=0.9, E=0.3, C=0.9, ask_price=0.50)
    assert r.decision == StoppingDecision.SKIP
    assert r.reason == "expired"


def test_negative_edge_skips():
    eng = _engine()
    r = eng.evaluate("w4", sso=1000.0, ttc_s=1200.0, I=0.8, E=-0.05, C=0.9, ask_price=0.50)
    assert r.decision == StoppingDecision.SKIP
    assert r.reason == "negative_edge"


def test_e_zero_defaults_to_wait():
    """Phase-3 placeholder: E=0 -> R=0 -> never ENTER -> safe WAIT."""
    eng = _engine()
    r = eng.evaluate("w5", sso=1000.0, ttc_s=1200.0, I=0.8, E=0.0, C=0.9, ask_price=0.50)
    assert r.decision == StoppingDecision.WAIT


def test_declining_R_three_ticks_skips():
    eng = _engine()
    # feed 4 strictly-decreasing E (hence R) values on the same window within a sniper state
    decision = None
    for e in (0.20, 0.15, 0.10, 0.05):
        r = eng.evaluate("w6", sso=1000.0, ttc_s=1200.0, I=0.8, E=e, C=0.9, ask_price=0.50)
        decision = r
    assert decision.decision == StoppingDecision.SKIP
    assert decision.reason == "rank_declining_3_ticks"


def test_positive_edge_velocity_lowers_effective_r_min():
    cfg = PRISMConfig()
    # An R that clears the boosted threshold but not the base sniper threshold.
    # Rising E gives v_E > 0; two ticks with increasing E on a sniper-state window.
    eng = StoppingEngine(cfg)
    eng.evaluate("wv", sso=1000.0, ttc_s=1200.0, I=0.75, E=0.10, C=0.85, ask_price=0.50)
    r = eng.evaluate("wv", sso=1000.0, ttc_s=1200.0, I=0.75, E=0.16, C=0.85, ask_price=0.50)
    # base sniper r_min = 0.12; boosted = 0.12*(1-0.15) = 0.102; R = 0.75*0.16*0.85 = 0.102
    assert r.v_E > 0
    assert r.eff_r_min < cfg.r_min_sniper
    assert r.decision == StoppingDecision.ENTER


def test_no_entry_tier_in_watching_state():
    eng = _engine()
    # WATCHING (sso<180) has no entry tier even with a high R -> WAIT (not ENTER).
    r = eng.evaluate("w7", sso=60.0, ttc_s=3500.0, I=0.9, E=0.3, C=0.95, ask_price=0.50)
    assert r.tier == "none"
    assert r.decision == StoppingDecision.WAIT


def test_harvester_tier_in_tier1_ready():
    eng = _engine()
    # TIER1_READY (180-720s) -> harvester tier; small R clears harvester r_min (0.03).
    r = eng.evaluate("w8", sso=300.0, ttc_s=3300.0, I=0.85, E=0.05, C=0.85, ask_price=0.50)
    assert r.tier == "harvester"


def test_optimal_stopping_decide_pure_function():
    from engine.pulse.prism.stopping import StoppingState
    st = StoppingState(seconds_since_open=1000.0, ttc_s=1200.0, I=0.8, E=0.2, C=0.9,
                       R=0.8 * 0.2 * 0.9, v_E=0.0, state_fsm=state_from_seconds_since_open(1000.0),
                       ask_price=0.50)
    r = optimal_stopping_decide(st, PRISMConfig())
    assert r.decision == StoppingDecision.ENTER


def test_report_counts_decisions():
    eng = _engine()
    eng.evaluate("a", sso=100.0, ttc_s=3500.0, I=0.2, E=0.0, C=0.5, ask_price=0.5)   # wait
    eng.evaluate("b", sso=3100.0, ttc_s=100.0, I=0.9, E=0.3, C=0.9, ask_price=0.5)   # skip
    rep = eng.to_report()
    assert rep["enabled"] is True
    assert rep["total_decisions"] == 2
    assert rep["counts"]["wait"] >= 1 and rep["counts"]["skip"] >= 1


# --------------------------------------------------------------------------------------------- #
# Engine integration: restrict-only gate on the LEGACY directional path (E=0 -> WAIT).
# --------------------------------------------------------------------------------------------- #

def test_engine_prism_stopping_gate_waits_early_hour(tmp_path):
    from engine.pulse.engine import PulseConfig, PulseEngine
    from engine.pulse.fair_value import RollingVol
    from engine.pulse.markets import PulseWindow
    from engine.pulse.price import PulsePriceFeed

    t0 = 2_000_000.0
    # a 1h window that opens at t0
    win = PulseWindow(event_id="e1", market_id="m1", slug="btc-up-or-down-hourly-2000000",
                      title="Bitcoin Up or Down", open_ts=t0, close_ts=t0 + 3600,
                      up_token_id="U", down_token_id="D", window_seconds=3600)

    price = {"p": 64000.0}

    def fetch():
        price["p"] += 3.0
        return price["p"]

    class _Mkt:
        def active_windows(self, now=None, **kw):
            return [win]
        def hydrate_books(self, w):
            pass
        def resolve_up(self, *a, **k):
            return None

    feed = PulsePriceFeed(fetcher=fetch, vol=RollingVol(window_s=900, min_samples=8),
                          max_open_lag_s=600.0)
    eng = PulseEngine(
        PulseConfig(
            tick_seconds=1.0, size_usd=10.0, min_edge=0.01, edge_buffer=0.0, basis_buffer=0.0,
            min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
            directional_down_only=True,
            prism_stopping_enabled=True,          # Phase 3 gate ON for this test (legacy path)
            data_dir=str(tmp_path), fresh_start=True,
        ),
        market_feed=_Mkt(), price_feed=feed)

    for i in range(12):                          # warm vol before the window opens
        eng.tick(now=t0 - 12 + i)
    # tick early in the hour (sso ~30s) — E=0 in phase 3 -> optimal stopping must WAIT
    eng.tick(now=t0 + 30)

    rbs = eng.reconciler.report()["rejected_by_stage"]
    assert rbs.get("prism_stopping", 0) >= 1
    assert eng.ledger.trades == 0                # no fill: gate held the candidate
    rep = eng.status()["prism_stopping"]
    assert rep["enabled"] is True and rep["counts"]["wait"] >= 1
