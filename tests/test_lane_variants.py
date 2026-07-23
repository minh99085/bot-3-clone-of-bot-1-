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


def test_registry_v2_variants_and_controls():
    # 9 variants for 10 lanes: lane01 AND lane02 both run "baseline" —
    # lane02_autonomy is the full-stack twin (pure mode off), B1 A/B.
    assert len(LANES) == 9
    assert "random_null" in LANES      # null control (kept, prereg)
    assert LANES["baseline"].q_mode == "barrier"
    # v2: drift family replaces the proven-loser fade lanes.
    for name in ("drift_barrier", "fav_cont_70", "fav_cont_80",
                 "fav_cont_depth", "fav_cont_open", "drift_garch"):
        assert name in LANES, name
    assert LANES["fav_cont_70"].require_momentum_agree is True
    assert LANES["fav_cont_70"].min_side_price == pytest.approx(0.70)
    # retired: anti-signal fades + finished negative control
    for gone in ("longshot_only", "late_window", "market_sigma_gap",
                 "depth_aware", "favorite_only", "legacy_ensemble",
                 "chainlink_ref"):
        assert gone not in LANES, gone


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


def test_entry_gates_fav_cont():
    fav = LANES["fav_cont_70"]
    # favorite + agreeing momentum + late window → allowed
    ok, _ = entry_allows(side_price=0.75, seconds_remaining=400, liquidity_usd=5000,
                         spec=fav, momentum=0.3, side_is_up=True)
    assert ok
    # cheap side blocked by the lane's own floor
    bad, reason = entry_allows(side_price=0.25, seconds_remaining=400, liquidity_usd=5000,
                               spec=fav, momentum=0.3, side_is_up=True)
    assert not bad and ("side_price" in reason or "cheap_ticket" in reason)
    # too early in the window
    bad, reason = entry_allows(side_price=0.75, seconds_remaining=700, liquidity_usd=5000,
                               spec=fav, momentum=0.3, side_is_up=True)
    assert not bad and "too_early" in reason

    depth = LANES["fav_cont_depth"]
    bad, reason = entry_allows(side_price=0.75, seconds_remaining=400, liquidity_usd=100,
                               spec=depth, momentum=0.3, side_is_up=True)
    assert not bad and "thin_book" in reason


def test_momentum_agreement_gate():
    fav = LANES["fav_cont_70"]
    # flat tape → no confirmation
    bad, reason = entry_allows(side_price=0.75, seconds_remaining=400, liquidity_usd=5000,
                               spec=fav, momentum=0.01, side_is_up=True)
    assert not bad and "no_momentum_confirmation" in reason
    # opposing tape → the 2.8%-WR against-the-tape case, blocked
    bad, reason = entry_allows(side_price=0.75, seconds_remaining=400, liquidity_usd=5000,
                               spec=fav, momentum=-0.4, side_is_up=True)
    assert not bad and "momentum_opposes" in reason
    # DOWN side with falling tape → allowed
    ok, _ = entry_allows(side_price=0.75, seconds_remaining=400, liquidity_usd=5000,
                         spec=fav, momentum=-0.4, side_is_up=False)
    assert ok
    # lanes without the flag are unaffected by momentum
    ok, _ = entry_allows(side_price=0.5, seconds_remaining=400, liquidity_usd=5000,
                         spec=LANES["baseline"], momentum=-0.4, side_is_up=True)
    assert ok


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


def test_drift_lane_q_source_and_direction(monkeypatch):
    """Rising history → drift lane's q ABOVE the driftless barrier's q."""
    monkeypatch.setenv(ENV_VAR, "drift_barrier")
    rising = [64000.0 * (1 + 0.00002 * i) for i in range(120)]
    _hist(monkeypatch, rising)
    q_drift, features, meta = mp.compute_cex_implied_up(
        momentum=0.3, timeframe="15m", pm_implied_up=0.60, spot=rising[-1],
        asset="ETH", seconds_to_resolution=400, strike=64000.0,
        slug="btc-updown-15m-1784000001",
    )
    assert meta["model_q_source"] == "barrier_drift_open"
    assert features["drift_mu_ann"] > 0
    assert features["advanced_q"] == pytest.approx(q_drift)

    monkeypatch.setenv(ENV_VAR, "baseline")
    q_plain, _, meta2 = mp.compute_cex_implied_up(
        momentum=0.3, timeframe="15m", pm_implied_up=0.60, spot=rising[-1],
        asset="ETH", seconds_to_resolution=400, strike=64000.0,
        slug="btc-updown-15m-1784000001",
    )
    assert meta2["model_q_source"] == "barrier_cex_open"
    assert q_drift >= q_plain  # positive drift can only raise P(close>strike)


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
