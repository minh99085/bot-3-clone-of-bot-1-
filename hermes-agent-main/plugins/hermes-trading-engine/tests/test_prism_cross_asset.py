"""Tests for PRISM Phase 6 — cross-asset lead-lag prior transfer (PAPER ONLY)."""

from engine.pulse.prism.cross_asset import (
    LEAD_LAG_SEC,
    apply_cross_asset_prior,
    transfer_posterior,
)


def test_lead_lag_table():
    assert LEAD_LAG_SEC["btc"] == 0.0
    assert LEAD_LAG_SEC["eth"] < LEAD_LAG_SEC["sol"] < LEAD_LAG_SEC["xrp"]


def test_self_transfer_zero():
    assert transfer_posterior("btc", "btc", 0.9) == 0.0


def test_bullish_leader_positive_transfer():
    assert transfer_posterior("btc", "eth", 0.9) > 0.0
    assert transfer_posterior("btc", "eth", 0.1) < 0.0          # bearish leader -> negative
    assert transfer_posterior("btc", "eth", 0.5) == 0.0         # no conviction -> zero


def test_transfer_decays_with_lag():
    eth = transfer_posterior("btc", "eth", 0.9)                 # lag 5s
    xrp = transfer_posterior("btc", "xrp", 0.9)                 # lag 45s
    assert eth > xrp > 0.0                                      # nearer follower gets more


def test_apply_prior_clamped_and_summed():
    p = apply_cross_asset_prior(0.50, {"btc": 0.9, "sol": 0.8}, "eth")
    assert 0.50 < p < 1.0                                       # bullish leaders push prior up
    # extreme leaders never push prior out of (0,1)
    p_hi = apply_cross_asset_prior(0.999, {"btc": 0.999}, "eth")
    p_lo = apply_cross_asset_prior(0.001, {"btc": 0.001}, "eth")
    assert 0.0 < p_hi < 1.0 and 0.0 < p_lo < 1.0


def test_apply_prior_no_leaders_is_identity():
    assert abs(apply_cross_asset_prior(0.42, {}, "eth") - 0.42) < 1e-9
