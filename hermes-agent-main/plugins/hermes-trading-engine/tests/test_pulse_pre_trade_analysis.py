"""Tests for dynamic pre-trade analysis (all-data synthesis before fill)."""

from __future__ import annotations

import pytest

from engine.pulse.pre_trade_analysis import (
    PreTradeEvidence,
    PreTradeGate,
    analyze_pre_trade,
    dynamic_council_thresholds,
    readiness_bucket,
)


def test_analyze_scores_high_when_edge_alignment_timing_strong():
    out = analyze_pre_trade(
        fair_p_up=0.62,
        poly_yes=0.48,
        council_views={"quant": 0.61, "grok": 0.60, "claude": 0.59},
        proposed_side="up",
        proposed_p_up=0.60,
        edge_snap={
            "pulse_edge_score": 0.72,
            "cex_momentum": {"basket_direction": "up", "exchange_agreement": 0.8},
        },
        ttc_s=2800.0,
        window_seconds=3600,
        seconds_since_open=800.0,
        spread=0.02,
        ask_depth_usd=500.0,
        price_fresh=True,
        vol_trusted=True,
        up_ask=0.45,
        down_ask=0.55,
        hourly_min_minutes=12.0,
    )
    assert out["score"] >= 0.55
    assert out["recommendation"] in ("trade", "cautious")
    assert out["components"]["member_alignment"] is not None
    assert out["components"]["timing_fit"] >= 0.7


def test_analyze_penalizes_early_1h_entry_before_12m():
    early = analyze_pre_trade(
        fair_p_up=0.58, poly_yes=0.50, council_views={"quant": 0.57},
        proposed_side="up", ttc_s=3300.0, window_seconds=3600,
        seconds_since_open=300.0, spread=0.02, ask_depth_usd=400.0,
        up_ask=0.42, hourly_min_minutes=12.0,
    )
    late = analyze_pre_trade(
        fair_p_up=0.58, poly_yes=0.50, council_views={"quant": 0.57},
        proposed_side="up", ttc_s=2400.0, window_seconds=3600,
        seconds_since_open=900.0, spread=0.02, ask_depth_usd=400.0,
        up_ask=0.42, hourly_min_minutes=12.0,
    )
    assert late["components"]["timing_fit"] > early["components"]["timing_fit"]
    assert late["score"] >= early["score"]


def test_dynamic_thresholds_raise_bar_when_readiness_low():
    low = dynamic_council_thresholds(
        {"score": 0.35}, base_margin=0.05, base_agreement=0.62,
        margin_boost_max=0.04, agreement_boost_max=0.06)
    high = dynamic_council_thresholds(
        {"score": 1.0}, base_margin=0.05, base_agreement=0.62,
        margin_boost_max=0.04, agreement_boost_max=0.06)
    assert low["effective_margin"] > high["effective_margin"]
    assert low["effective_agreement"] > high["effective_agreement"]
    assert high["effective_margin"] == pytest.approx(0.05)
    assert high["effective_agreement"] == pytest.approx(0.62)


def test_gate_rejects_low_readiness_blocks_proven_losing_bucket():
    gate = PreTradeGate(enabled=True, min_score=0.45, exploration_rate=0.0, seed=1)
    ev = PreTradeEvidence()
    for _ in range(30):
        ev.record("0.40-0.48", won=False, pnl=-1.0)
    bad = analyze_pre_trade(
        fair_p_up=0.52, poly_yes=0.50, ttc_s=200.0, window_seconds=300,
        seconds_since_open=60.0, proposed_side="up")
    bad["score"] = 0.42
    bad["recommendation"] = "wait"
    res = gate.evaluate(bad, evidence=ev)
    assert res["decision"] == "reject"
    assert "bad_readiness_bucket" in res["reasons"][0] or "pre_trade_low_readiness" in res["reasons"][0]


def test_gate_size_scale_never_exceeds_one():
    gate = PreTradeGate(enabled=True, min_size_scale=0.35)
    good = {"score": 0.9, "recommendation": "trade"}
    assert gate.size_scale(good) <= 1.0
    assert gate.size_scale(good) >= 0.35


def test_analyze_includes_tv_2h_alignment_when_review_present():
    review = {
        "enabled": True,
        "alert_count": 5,
        "trend_direction": "up",
        "confidence": 0.8,
        "aligned": True,
    }
    aligned = analyze_pre_trade(
        fair_p_up=0.58, poly_yes=0.50, proposed_side="up",
        ttc_s=2400.0, window_seconds=3600, seconds_since_open=900.0,
        spread=0.02, ask_depth_usd=400.0, up_ask=0.42,
        tv_2h_review=review,
    )
    mis = analyze_pre_trade(
        fair_p_up=0.58, poly_yes=0.50, proposed_side="down",
        ttc_s=2400.0, window_seconds=3600, seconds_since_open=900.0,
        spread=0.02, ask_depth_usd=400.0, down_ask=0.58,
        tv_2h_review=review,
    )
    assert aligned["components"]["tv_2h_alignment"] is not None
    assert mis["components"]["tv_2h_alignment"] is not None
    assert aligned["components"]["tv_2h_alignment"] > mis["components"]["tv_2h_alignment"]


def test_analyze_exposes_tv_segment_scores():
    review = {
        "enabled": True,
        "alert_count": 6,
        "trend_direction": "up",
        "confidence": 0.7,
        "aligned": True,
        "segments": {
            "open_regime": {
                "alert_count": 2, "trend_direction": "down",
                "confidence": 0.6, "aligned": False, "divergent": True,
            },
            "actionable_trend": {
                "alert_count": 3, "trend_direction": "up",
                "confidence": 0.8, "aligned": True, "divergent": False,
            },
            "lookback_tail": {
                "alert_count": 6, "trend_direction": "up",
                "confidence": 0.7, "aligned": True, "divergent": False,
            },
        },
    }
    out = analyze_pre_trade(
        fair_p_up=0.58, poly_yes=0.50, proposed_side="up",
        ttc_s=2400.0, window_seconds=3600, seconds_since_open=1200.0,
        spread=0.02, ask_depth_usd=400.0, up_ask=0.42,
        tv_2h_review=review,
    )
    segs = out.get("tv_segment_scores")
    assert segs is not None
    assert segs["actionable_trend"]["weight"] == 0.50
    assert segs["open_regime"]["weight"] == 0.30
    assert "tv_inband" in out["summary"] or "tv2h" in out["summary"]
    # actionable UP should score higher than open_regime DOWN for side=up
    assert segs["actionable_trend"]["score"] > segs["open_regime"]["score"]


def test_readiness_bucket_labels():
    assert readiness_bucket(0.30) == "<0.40"
    assert readiness_bucket(0.55) == "0.48-0.62"
    assert readiness_bucket(0.70) == ">=0.62"
