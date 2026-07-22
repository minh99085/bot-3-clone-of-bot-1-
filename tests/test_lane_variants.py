"""10-lane experiment — variant registry, gates, and mispricing wiring."""

from __future__ import annotations

import pytest

import hermes.mispricing as mp
from hermes.lane_variants import (
    ENV_VAR,
    LANES,
    active_spec,
    entry_allows,
    random_q_for,
)


def test_registry_has_nine_variants_with_controls():
    # 9 variants for 10 lanes: lane01 AND lane02 both run "baseline" —
    # lane02_autonomy is the full-stack twin (pure mode off), B1 A/B.
    assert len(LANES) == 9
    assert "legacy_ensemble" in LANES  # negative control
    assert "random_null" in LANES      # null control
    assert LANES["baseline"].q_mode == "barrier"
    assert LANES["market_sigma_gap"].sigma_kind == "market_implied"
    assert "chainlink_ref" not in LANES  # dead with the paid-oracle pivot


def test_unknown_variant_falls_back_to_baseline(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "does_not_exist")
    spec = active_spec()
    assert spec.q_mode == "barrier" and spec.sigma_kind == "realized"
    assert "baseline" in spec.name


def test_random_q_is_deterministic_and_uninformative():
    q1 = random_q_for("btc-updown-15m-1784000000", 0.60)
    q2 = random_q_for("btc-updown-15m-1784000000", 0.60)
    assert q1 == q2  # reproducible across lanes/restarts
    # ~half the windows go each way
    sides = [random_q_for(f"btc-updown-15m-{i}", 0.5) > 0.5 for i in range(200)]
    assert 60 < sum(sides) < 140


def test_entry_gates():
    fav = LANES["favorite_only"]
    ok, _ = entry_allows(side_price=0.72, seconds_remaining=400, liquidity_usd=5000, spec=fav)
    assert ok
    bad, reason = entry_allows(side_price=0.25, seconds_remaining=400, liquidity_usd=5000, spec=fav)
    assert not bad and "side_price" in reason

    late = LANES["late_window"]
    bad, reason = entry_allows(side_price=0.5, seconds_remaining=700, liquidity_usd=5000, spec=late)
    assert not bad and "too_early" in reason
    ok, _ = entry_allows(side_price=0.5, seconds_remaining=200, liquidity_usd=5000, spec=late)
    assert ok

    depth = LANES["depth_aware"]
    bad, reason = entry_allows(side_price=0.5, seconds_remaining=400, liquidity_usd=100, spec=depth)
    assert not bad and "thin_book" in reason


def _hist(monkeypatch, prices):
    times = [float(i) for i in range(len(prices))]
    monkeypatch.setattr(
        mp, "get_asset_price_history", lambda asset, max_points=240: (times, list(prices))
    )


def test_random_lane_q_flows_through_compute(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "random_null")
    _hist(monkeypatch, [64000.0 + i for i in range(30)])
    q, features, meta = mp.compute_cex_implied_up(
        momentum=0.3, timeframe="15m", pm_implied_up=0.60, spot=64100.0,
        asset="ETH", seconds_to_resolution=400, strike=64000.0,
        slug="btc-updown-15m-1784000000",
    )
    assert meta["model_q_source"] == "random_null"
    assert q == pytest.approx(random_q_for("btc-updown-15m-1784000000", 0.60))
    assert features["advanced_q"] == pytest.approx(q)


def test_legacy_lane_skips_barrier_even_with_strike(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "legacy_ensemble")
    _hist(monkeypatch, [64000.0 + i for i in range(30)])
    q, features, meta = mp.compute_cex_implied_up(
        momentum=0.3, timeframe="15m", pm_implied_up=0.60, spot=64100.0,
        asset="ETH", seconds_to_resolution=400, strike=64000.0,
        slug="btc-updown-15m-1784000001",
    )
    assert meta["model_q_source"].startswith("legacy_")
    assert "barrier_q" not in features


def test_market_sigma_lane_uses_calibrated_sigma(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "market_sigma_gap")
    mp._SIGMA_RATIO_EWMA.clear()
    _hist(monkeypatch, [64000.0 + i for i in range(30)])
    from strategy.advanced_signals import implied_sigma_ann

    pm, spot, strike, tau = 0.70, 64100.0, 64000.0, 400.0
    expected_sigma = implied_sigma_ann(pm, spot, strike, tau)
    q, features, meta = mp.compute_cex_implied_up(
        momentum=0.3, timeframe="15m", pm_implied_up=pm, spot=spot,
        asset="ETH", seconds_to_resolution=tau, strike=strike,
        slug="btc-updown-15m-1784000002",
    )
    assert meta["model_q_source"] == "barrier_cex_open"
    # First observation: ratio EWMA seeds at implied/realized, so
    # σ* = ratio × realized == this window's implied σ exactly.
    assert features["barrier_sigma_ann"] == pytest.approx(expected_sigma, rel=1e-6)
    # With market-consistent σ and the same spot, the barrier ≈ the market:
    # any residual gap comes only from spot/strike freshness.
    assert abs(q - pm) < 0.02


def test_sigma_ratio_ewma_learns_across_windows(monkeypatch):
    mp._SIGMA_RATIO_EWMA.clear()
    r1 = mp.update_sigma_ratio("BTC", implied=1.2, realized=0.6)  # ratio 2.0 seeds
    assert r1 == pytest.approx(2.0)
    r2 = mp.update_sigma_ratio("BTC", implied=0.6, realized=0.6)  # ratio 1.0 obs
    assert r2 == pytest.approx(0.95 * 2.0 + 0.05 * 1.0)
    # Bad inputs don't move it
    assert mp.update_sigma_ratio("BTC", implied=0.0, realized=0.6) == pytest.approx(r2)
    assert mp.sigma_ratio("ETH") == 1.0  # unseen asset → neutral


def test_garch_lane_uses_garch_sigma(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "garch_sigma")
    # noisy path so GARCH has variance to chew on
    import math

    prices = [64000.0 * (1 + 0.0004 * math.sin(i * 1.7)) for i in range(60)]
    _hist(monkeypatch, prices)
    q, features, meta = mp.compute_cex_implied_up(
        momentum=0.3, timeframe="15m", pm_implied_up=0.60, spot=64100.0,
        asset="ETH", seconds_to_resolution=400, strike=64000.0,
        slug="btc-updown-15m-1784000003",
    )
    assert meta["model_q_source"] == "barrier_cex_open"
    from strategy.advanced_signals import garch_sigma_ann

    assert features["barrier_sigma_ann"] == pytest.approx(
        garch_sigma_ann(prices, sample_sec=1.0), rel=1e-6
    )


def test_baseline_unchanged_by_default(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    _hist(monkeypatch, [64000.0 + i for i in range(30)])
    q, features, meta = mp.compute_cex_implied_up(
        momentum=0.3, timeframe="15m", pm_implied_up=0.60, spot=64100.0,
        asset="ETH", seconds_to_resolution=400, strike=64000.0,
        slug="btc-updown-15m-1784000004",
    )
    assert meta["model_q_source"] == "barrier_cex_open"
    assert features["advanced_q"] == pytest.approx(q)
