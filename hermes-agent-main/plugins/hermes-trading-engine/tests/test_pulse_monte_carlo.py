"""Tests for directional MC + p_exec self-tune (dep-arb MC removed)."""

from __future__ import annotations

import pytest

from engine.pulse.monte_carlo import (
    HAVE_NUMPY, closed_form_digital_p_up, mc_digital_p_up, mc_directional_p_up,
    simulate_prices_at_times, validate_scenario_params, NEUTRAL_SCENARIO,
)
from engine.pulse.p_exec import (
    ContextSelfTune, blend_p, compute_p_exec, context_key, wilson_lb,
)

pytestmark = pytest.mark.skipif(not HAVE_NUMPY, reason="numpy required for MC")


def test_mc_digital_converges_to_closed_form():
    sigma = 7e-5
    for s_now, s_open, secs in [(60000, 60000, 300), (60050, 60000, 300), (59950, 60000, 600)]:
        cf = closed_form_digital_p_up(s_now, s_open, sigma, secs)
        mc = mc_digital_p_up(s_now, s_open, sigma, secs, n_paths=60000, seed=7)
        assert abs(mc - cf) < 0.02, (s_now, s_open, secs, mc, cf)


def test_mc_directional_available_and_near_digital():
    out = mc_directional_p_up(60100, 60000, 7e-5, 300, n_paths=20000, seed=11,
                              control_alpha=0.5)
    assert out["available"] is True
    assert 0.0 <= out["p_mc"] <= 1.0
    assert out["p_crash"] is not None
    assert abs(out["p_mc_adj"] - out["p_digital"]) < 0.05


def test_mc_directional_jumps_change_p():
    base = mc_directional_p_up(60200, 60000, 7e-5, 300, n_paths=30000, seed=3)
    jmp = mc_directional_p_up(60200, 60000, 7e-5, 300, n_paths=30000, seed=3,
                              jump_intensity_per_sec=0.02, jump_sigma=0.002)
    assert jmp["p_mc"] < base["p_mc"]


def test_shared_path_prices_shape():
    now = 1_000_000.0
    prices, idx = simulate_prices_at_times(60000, now, [now + 600, now + 300, now + 900],
                                           7e-5, n_paths=1000, rng=None)
    assert prices.shape == (1000, 3)
    assert set(idx.keys()) == {now + 300, now + 600, now + 900}


def test_validate_scenario_clamps_and_lean():
    p = validate_scenario_params({
        "sigma_mult": 9.0, "mu_per_sec": 1.0, "lean": "UP",
        "crash_threshold_pct": 99, "confidence": 2.0,
    }, source="grok")
    assert p["sigma_mult"] == 2.0
    assert p["lean"] == "up"
    assert p["crash_threshold_pct"] == 5.0
    assert p["confidence"] == 1.0
    assert NEUTRAL_SCENARIO["lean"] == "none"


def test_context_key_and_p_exec():
    k = context_key(asset="btc", horizon="15m", ttc_s=400, vwap=0.58, sso_s=200)
    assert "btc|15m|" in k
    assert "55_65" in k
    pb = blend_p(p_mkt=0.55, p_digital=0.60, p_mc=0.58)
    assert 0.55 <= pb <= 0.60
    pe = compute_p_exec(p_blend=0.55, wr_emp=0.70, n_c=80, n0=40)
    assert pe > 0.55


def test_context_self_tune_promote():
    st = ContextSelfTune(min_promote_n=10, min_demote_n=8, margin=0.01)
    k = "btc|15m|mid|55_65|na|none"
    for i in range(12):
        st.record(k, won=True, pnl=4.0, vwap=0.55, p_blend=0.60, p_mkt=0.55, p_mc=0.58)
    assert st.is_promoted(k)
    ok, reason = st.allow_trade(k)
    assert ok and reason == "context_promoted"
