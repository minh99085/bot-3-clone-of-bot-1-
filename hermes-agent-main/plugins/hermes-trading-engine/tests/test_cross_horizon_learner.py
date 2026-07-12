"""Tests for CrossHorizonLearner (15m ↔ 1h shared restrict/size policy)."""

from engine.pulse.cross_horizon_learner import (
    CrossHorizonConfig,
    CrossHorizonLearner,
    CrossHorizonPolicy,
    classify_horizon,
    sso_frac,
    timing_band,
)


def test_classify_horizon():
    assert classify_horizon(window_seconds=900, series_slug="btc-up-or-down-15m") == "15m"
    assert classify_horizon(window_seconds=3600, series_slug="btc-up-or-down-hourly") == "1h"
    assert classify_horizon(window_seconds=300) == "other"


def test_timing_bands():
    assert timing_band(0.05) == "early"
    assert timing_band(0.30) == "mid"
    assert timing_band(0.80) == "late"
    assert abs(sso_frac(450, 900) - 0.5) < 1e-9


def test_disabled_noop():
    ln = CrossHorizonLearner(CrossHorizonConfig(enabled=False))
    ln.record_settled(won=True, pnl_usd=3.0, horizon="15m", side="down", sso=300, window_seconds=900)
    assert ln.maybe_adjust() is None
    ev = ln.evaluate_entry(horizon="1h", side="up", sso=100, ttc_s=3500, window_seconds=3600)
    assert ev["decision"] == "pass"
    assert ev["size_mult"] == 1.0


def test_promote_15m_mid_to_1h_sso_floor():
    ln = CrossHorizonLearner(CrossHorizonConfig(
        enabled=True, min_samples=8, min_bucket_n=6, cooldown_settlements=6,
        breakeven_wr=0.50, target_wr=0.70, kill_wr=0.40, exploration_rate=0.0,
        transfer_sso_frac_lo=0.15,
    ))
    # Mid-window 15m winners → promote 1h SSO floor
    for i in range(10):
        ln.record_settled(
            won=True, pnl_usd=2.0, horizon="15m", side="down",
            entry_price=0.55, sso=300.0, ttc_s=600.0, window_seconds=900.0,
            now=1_700_000_000.0 + i,
        )
    action = ln.maybe_adjust()
    assert action is not None
    assert "promote_15m_mid→1h_sso" in action
    assert ln.policy.h1_min_sso_frac == 0.15

    # Early 1h entry rejected by floor
    early = ln.evaluate_entry(
        horizon="1h", side="down", sso=200.0, ttc_s=3400.0, window_seconds=3600.0,
        explore_rng=_FixedRng(0.99),
    )
    assert early["decision"] == "reject"
    assert early["reason"] == "xh_1h_sso_frac_floor"

    mid = ln.evaluate_entry(
        horizon="1h", side="down", sso=900.0, ttc_s=2700.0, window_seconds=3600.0,
    )
    assert mid["decision"] == "pass"


def test_promote_15m_down_prefer_on_1h():
    ln = CrossHorizonLearner(CrossHorizonConfig(
        enabled=True, min_samples=8, min_bucket_n=6, cooldown_settlements=6,
        target_wr=0.55, breakeven_wr=0.90, kill_wr=0.20, exploration_rate=0.0,
    ))
    # DOWN dominates 15m; UP mediocre — prefer DOWN transfer to 1h
    for i in range(8):
        ln.record_settled(
            won=True, pnl_usd=3.0, horizon="15m", side="down",
            sso=400.0, window_seconds=900.0, now=1_700_000_000.0 + i,
        )
    for i in range(6):
        ln.record_settled(
            won=(i % 2 == 0), pnl_usd=(1.0 if i % 2 == 0 else -2.0),
            horizon="15m", side="up",
            sso=400.0, window_seconds=900.0, now=1_700_000_100.0 + i,
        )
    action = ln.maybe_adjust()
    assert action is not None
    assert "promote_15m_down→1h" in action
    assert ln.policy.h1_prefer_down is True
    up = ln.evaluate_entry(horizon="1h", side="up", sso=900.0, ttc_s=2700.0, window_seconds=3600.0)
    down = ln.evaluate_entry(horizon="1h", side="down", sso=900.0, ttc_s=2700.0, window_seconds=3600.0)
    assert up["decision"] == "pass"
    assert down["decision"] == "pass"
    assert up["size_mult"] < down["size_mult"]


def test_demote_1h_up_bleeds_to_both():
    ln = CrossHorizonLearner(CrossHorizonConfig(
        enabled=True, min_samples=4, min_bucket_n=4, cooldown_settlements=4,
        kill_wr=0.50, target_wr=0.90, breakeven_wr=0.90, exploration_rate=0.0,
    ))
    for i in range(6):
        ln.record_settled(
            won=False, pnl_usd=-3.0, horizon="1h", side="up",
            sso=200.0, window_seconds=3600.0, now=1_700_000_000.0 + i,
        )
    action = ln.maybe_adjust()
    assert action is not None
    assert "demote_1h_up→both" in action
    assert ln.policy.h1_block_early_up is True
    assert ln.policy.m15_block_early_up is True
    rej = ln.evaluate_entry(
        horizon="1h", side="up", sso=200.0, ttc_s=3400.0, window_seconds=3600.0,
        explore_rng=_FixedRng(0.99),
    )
    assert rej["decision"] == "reject"
    assert "xh_1h_block_early_up" in (rej.get("reasons") or [rej.get("reason")])


def test_exploration_carveout():
    ln = CrossHorizonLearner(CrossHorizonConfig(enabled=True, exploration_rate=1.0))
    ln.policy = CrossHorizonPolicy(h1_block_early_up=True)
    ev = ln.evaluate_entry(
        horizon="1h", side="up", sso=100.0, ttc_s=3500.0, window_seconds=3600.0,
        explore_rng=_FixedRng(0.0),
    )
    assert ev["decision"] == "explore"
    assert ln._explored == 1


def test_state_roundtrip():
    ln = CrossHorizonLearner(CrossHorizonConfig(enabled=True, min_samples=2, cooldown_settlements=1))
    ln.record_settled(won=True, pnl_usd=1.0, horizon="15m", side="down", sso=300, window_seconds=900)
    ln.policy.h1_min_sso_frac = 0.2
    st = ln.to_state()
    ln2 = CrossHorizonLearner(CrossHorizonConfig(enabled=True))
    ln2.load_state(st)
    assert ln2.policy.h1_min_sso_frac == 0.2
    assert len(ln2._recent) == 1
    rep = ln2.report()
    assert rep["mode"] == "restrict_size_only_shared_policy"
    assert rep["execution_gate_still_authoritative"] is True


class _FixedRng:
    def __init__(self, value: float):
        self._v = float(value)

    def random(self) -> float:
        return self._v
