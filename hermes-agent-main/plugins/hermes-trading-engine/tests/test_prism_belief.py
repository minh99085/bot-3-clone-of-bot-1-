"""Tests for PRISM Phase 1 — Bayesian belief engine (PAPER ONLY)."""

import math

from engine.pulse.prism.belief import (
    DEFAULT_LIKELIHOOD_RATIOS,
    DEFAULT_SIGNAL_WEIGHTS,
    BeliefEngine,
    BeliefState,
    SignalObservation,
    freshness,
    logit,
    make_observation,
    sigmoid,
    signal_log_odds_shift,
    update_belief,
)


def test_logit_sigmoid_roundtrip():
    for p in (0.1, 0.37, 0.5, 0.62, 0.9):
        assert abs(sigmoid(logit(p)) - p) < 1e-9
    # boundary clamp: no exceptions, stays in (0, 1)
    assert 0.0 < sigmoid(logit(0.0)) < 1.0
    assert 0.0 < sigmoid(logit(1.0)) < 1.0


def test_freshness_decay_half_life():
    assert freshness(0.0, 60.0) == 1.0
    assert abs(freshness(60.0, 60.0) - 0.5) < 1e-9      # one half-life -> 0.5
    assert abs(freshness(120.0, 60.0) - 0.25) < 1e-9    # two half-lives -> 0.25
    assert freshness(1000.0, None) == 1.0               # never decays
    assert freshness(1000.0, math.inf) == 1.0
    assert 0.0 <= freshness(9999.0, 30.0) <= 1.0        # capped/floored


def test_agree_tv_signals_push_posterior_up():
    obs = [
        make_observation("tv_15m", direction=+1, strength=1.0, age_s=0.0),
        make_observation("tv_30m", direction=+1, strength=1.0, age_s=0.0),
        make_observation("cex_lead", direction=+1, strength=1.0, age_s=0.0),
        make_observation("quant_fair", direction=+1, strength=1.0, age_s=0.0),
    ]
    state = update_belief(0.50, obs)
    assert isinstance(state, BeliefState)
    assert state.posterior_p > 0.50
    assert state.n_signals_used == 4
    assert len(state.breakdown) == 4


def test_down_signals_push_posterior_down():
    obs = [
        make_observation("tv_15m", direction=-1, strength=1.0, age_s=0.0),
        make_observation("cex_lead", direction=-1, strength=1.0, age_s=0.0),
    ]
    state = update_belief(0.50, obs)
    assert state.posterior_p < 0.50


def test_conflict_pattern_pulls_toward_or_below_half():
    """A tv_conflict pattern (LR < 1) must lower the posterior vs the same up-signals alone."""
    up_only = [
        make_observation("tv_15m", direction=+1, strength=1.0),
        make_observation("tv_30m", direction=+1, strength=1.0),
    ]
    base = update_belief(0.50, up_only)
    with_conflict = update_belief(0.50, up_only + [
        # conflict oriented against the proposed (up) side, high strength so it dominates
        SignalObservation("tv_conflict", direction=+1, strength=1.0, half_life_s=None),
    ])
    assert with_conflict.posterior_p < base.posterior_p
    assert with_conflict.posterior_p <= 0.50 + 1e-9


def test_freshness_reduces_contribution():
    fresh = update_belief(0.50, [make_observation("cex_lead", +1, 1.0, age_s=0.0)])
    stale = update_belief(0.50, [make_observation("cex_lead", +1, 1.0, age_s=180.0)])
    assert fresh.posterior_p > stale.posterior_p > 0.50


def test_empty_observations_returns_prior():
    state = update_belief(0.63, [])
    assert abs(state.posterior_p - 0.63) < 1e-9
    assert state.n_signals_used == 0
    assert state.breakdown == []


def test_prior_zero_one_clamped():
    s0 = update_belief(0.0, [make_observation("tv_15m", +1, 1.0)])
    s1 = update_belief(1.0, [make_observation("tv_15m", -1, 1.0)])
    assert 0.0 < s0.posterior_p < 1.0
    assert 0.0 < s1.posterior_p < 1.0


def test_neutral_and_zero_strength_contribute_nothing():
    assert signal_log_odds_shift(make_observation("tv_15m", 0, 1.0)) == 0.0
    assert signal_log_odds_shift(make_observation("tv_15m", +1, 0.0)) == 0.0


def test_lr_table_pattern_uses_likelihood_ratio():
    # conviction_lean LR = 1.50 -> confirming (+1) shift == log(1.5) at full strength/fresh
    obs = SignalObservation("conviction_lean", direction=+1, strength=1.0, half_life_s=None)
    shift = signal_log_odds_shift(obs)
    assert abs(shift - math.log(1.50)) < 1e-9
    # a fade LR < 1 confirming the proposed side yields a negative shift
    fade = signal_log_odds_shift(
        SignalObservation("stale_polymarket_down", direction=+1, strength=1.0, half_life_s=None))
    assert fade < 0.0


def test_default_tables_present():
    assert set(DEFAULT_SIGNAL_WEIGHTS) >= {
        "chainlink_anchor", "cex_lead", "book_imbalance",
        "tv_15m", "tv_30m", "tv_60m", "tv_240m", "tv_1440m", "quant_fair",
    }
    assert DEFAULT_LIKELIHOOD_RATIOS["tv_conflict"] < 1.0
    assert DEFAULT_LIKELIHOOD_RATIOS["conviction_lean"] > 1.0


def test_belief_engine_load_save_roundtrip(tmp_path):
    eng = BeliefEngine(data_dir=tmp_path)
    eng.lr_table["tv_conflict"] = 0.42
    eng.record_outcome("tv_15_30_agree", won=True)
    eng.record_outcome("tv_15_30_agree", won=False)
    eng.save()
    assert (tmp_path / "prism_lr_table.json").exists()

    eng2 = BeliefEngine(data_dir=tmp_path)
    assert abs(eng2.lr_table["tv_conflict"] - 0.42) < 1e-9
    assert eng2.outcomes["tv_15_30_agree"] == {"wins": 1, "losses": 1}


def test_belief_engine_update_uses_own_table():
    eng = BeliefEngine()
    state = eng.update(0.50, [make_observation("tv_15m", +1, 1.0)])
    assert state.posterior_p > 0.50
    rep = eng.report()
    assert rep["enabled"] is True and rep["lr_table_size"] >= 9
