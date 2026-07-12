"""BTC-pulse factor/context model (Phase 5) — observe-only, edge_quality_score + buckets."""

from __future__ import annotations

from engine.pulse.factors import (compute_factors, edge_quality_bucket, FactorEngine,
                                   FactorSnapshot)


def test_edge_quality_bucket_thresholds():
    assert edge_quality_bucket(0.1) == "low" and edge_quality_bucket(0.5) == "medium"
    assert edge_quality_bucket(0.8) == "high" and edge_quality_bucket(None) == "na"


def test_good_conditions_score_higher_than_bad():
    good = compute_factors(poly_yes=0.5, spread=0.01, ask_depth_usd=400.0, bid_depth_usd=400.0,
                           ttc_s=200.0, signal={"direction": "up", "strength": 0.4,
                                                "realized_vol": 5e-5},
                           divergence=0.05, overlay_regime="calm")
    bad = compute_factors(poly_yes=0.97, spread=0.06, ask_depth_usd=20.0, bid_depth_usd=20.0,
                          ttc_s=8.0, signal={"direction": "neutral", "strength": 0.0},
                          divergence=0.0, overlay_regime="event_risk")
    assert good.edge_quality_score > bad.edge_quality_score
    assert good.observe_only is True
    assert "near_settlement_boundary" in bad.reason_codes or bad.settlement_boundary_risk > 0
    assert "grok_event_risk" in bad.reason_codes


def test_factors_safe_with_missing_data():
    f = compute_factors(poly_yes=None, spread=None, ask_depth_usd=None, bid_depth_usd=None,
                        ttc_s=None, signal=None, divergence=None, overlay_regime=None)
    assert f.observe_only is True and f.edge_quality_bucket in ("na", "low", "medium", "high")
    assert f.orderbook_imbalance is None and f.spread_liquidity_factor is None


def test_orderbook_imbalance_sign():
    f = compute_factors(poly_yes=0.5, spread=0.02, ask_depth_usd=100.0, bid_depth_usd=300.0,
                        ttc_s=150.0)
    assert f.orderbook_imbalance is not None and f.orderbook_imbalance > 0   # more bids than asks


def test_factor_engine_coverage_and_grouped_pnl():
    fe = FactorEngine()
    s1 = compute_factors(poly_yes=0.5, spread=0.01, ask_depth_usd=400.0, bid_depth_usd=400.0,
                         ttc_s=200.0, signal={"direction": "up", "strength": 0.4,
                                              "realized_vol": 5e-5}, divergence=0.06)
    fe.observe(s1)
    fe.record_settled(bucket=s1.edge_quality_bucket, pnl=7.5, won=True)
    fe.record_settled(bucket=s1.edge_quality_bucket, pnl=-5.0, won=False)
    r = fe.report()
    assert r["observe_only"] is True and r["affects_trading"] is False
    assert r["snapshots"] == 1
    g = r["pnl_by_edge_quality_bucket"][s1.edge_quality_bucket]
    assert g["n"] == 2 and g["win_rate"] == 0.5 and abs(g["pnl_usd"] - 2.5) < 1e-9
