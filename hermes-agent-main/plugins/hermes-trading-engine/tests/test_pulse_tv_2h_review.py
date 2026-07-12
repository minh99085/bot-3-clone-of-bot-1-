"""Tests for 2-hour TradingView trend review (observe-only, phase-segmented)."""

import time

from engine.pulse.tv_2h_review import (
    PRE_BAND_END_S,
    IN_BAND_END_S,
    alert_hourly_phase,
    compute_tv_2h_review,
    filter_alerts_in_lookback,
    segment_alerts_by_phase,
    segment_alignment_scores,
    tv_2h_alignment_score,
    tv_2h_trend_p_up,
    tv_ladder_alignment,
)


def _alerts(now, specs):
    """specs: (age_s, direction, price) — received_at = now - age."""
    return [
        {"received_at": now - age, "direction": d, "price": p}
        for age, d, p in specs
    ]


def _alerts_at_sso(hour_floor, specs):
    """specs: (sso_s, direction, price) — place alerts at hour_floor + sso."""
    return [
        {"received_at": hour_floor + sso, "direction": d, "price": p}
        for sso, d, p in specs
    ]


def test_filter_alerts_in_lookback():
    now = 1_000_000.0
    alerts = _alerts(now, [(100, "UP", 100), (5000, "DOWN", 99), (7000, "UP", 101)])
    kept = filter_alerts_in_lookback(alerts, now=now, lookback_s=7200)
    assert len(kept) == 3
    old = filter_alerts_in_lookback(alerts, now=now, lookback_s=3600)
    assert len(old) == 1
    assert old[0]["direction"] == "UP"


def test_alert_hourly_phase_boundaries():
    assert alert_hourly_phase(100.0) == "pre_band"
    assert alert_hourly_phase(PRE_BAND_END_S - 1) == "pre_band"
    assert alert_hourly_phase(PRE_BAND_END_S) == "in_band"
    assert alert_hourly_phase(1800.0) == "in_band"
    assert alert_hourly_phase(IN_BAND_END_S) == "in_band"
    assert alert_hourly_phase(IN_BAND_END_S + 1) == "post_band"
    assert alert_hourly_phase(3500.0) == "post_band"


def test_segment_alerts_by_phase():
    hour = 1_700_000_000.0  # arbitrary hour floor
    hour = hour - (hour % 3600)
    alerts = _alerts_at_sso(hour, [
        (60, "UP", 100),
        (1200, "DOWN", 99),
        (3000, "UP", 101),
    ])
    segs = segment_alerts_by_phase(alerts)
    assert len(segs["pre_band"]) == 1
    assert len(segs["in_band"]) == 1
    assert len(segs["post_band"]) == 1
    assert segs["pre_band"][0]["hourly_phase"] == "pre_band"


def test_compute_tv_2h_review_aligned_uptrend():
    now = time.time()
    alerts = _alerts(now, [
        (7000, "UP", 100.0),
        (5000, "UP", 101.0),
        (3000, "UP", 102.0),
        (1000, "UP", 103.0),
    ])
    rev = compute_tv_2h_review(
        alerts=alerts, now=now, lookback_s=7200, symbol="BTCUSD", oracle_price_now=104.0)
    assert rev["alert_count"] == 4
    assert rev["trend_direction"] == "up"
    assert rev["aligned"] is True
    assert rev["divergent"] is False
    assert rev["alignment"] == "aligned"
    assert rev["price_delta_pct"] is not None
    assert rev["price_delta_pct"] > 0
    assert rev["confidence"] > 0.4
    assert "segments" in rev
    assert set(rev["segments"]) >= {"open_regime", "actionable_trend", "lookback_tail", "post_band"}
    assert rev["entry_band_s"] == [900, 2700]


def test_compute_tv_2h_review_divergent():
    now = time.time()
    alerts = _alerts(now, [
        (6000, "UP", 100.0),
        (4000, "UP", 101.0),
        (2000, "UP", 102.0),
    ])
    rev = compute_tv_2h_review(
        alerts=alerts, now=now, lookback_s=7200, symbol="BTCUSD", oracle_price_now=95.0)
    assert rev["trend_direction"] == "up"
    assert rev["divergent"] is True
    assert rev["aligned"] is False
    assert rev["alignment"] == "divergent"


def test_segmented_review_roles():
    """Early alerts → open_regime; in-band → actionable; late → post_band."""
    hour = 1_800_000_000.0
    hour = hour - (hour % 3600)
    # Entry decision at :20 into hour
    now = hour + 1200
    alerts = _alerts_at_sso(hour, [
        (30, "DOWN", 100.0),     # pre_band early
        (120, "DOWN", 99.5),     # pre_band
        (1000, "UP", 99.0),      # in_band
        (1100, "UP", 99.2),      # in_band
    ])
    # Prior hour late alerts (in 2h lookback)
    prior = hour - 3600
    alerts += _alerts_at_sso(prior, [
        (3000, "DOWN", 101.0),   # post_band prior hour
        (3200, "DOWN", 100.5),
    ])
    rev = compute_tv_2h_review(
        alerts=alerts, now=now, lookback_s=7200, symbol="BTCUSD", oracle_price_now=99.5)
    assert rev["phase_counts"]["pre_band"] == 2
    assert rev["phase_counts"]["in_band"] == 2
    assert rev["phase_counts"]["post_band"] == 2
    assert rev["segments"]["open_regime"]["alert_count"] == 2
    assert rev["segments"]["open_regime"]["trend_direction"] == "down"
    assert rev["segments"]["actionable_trend"]["alert_count"] == 2
    assert rev["segments"]["actionable_trend"]["trend_direction"] == "up"
    assert rev["segments"]["post_band"]["alert_count"] == 2
    assert rev["segments"]["lookback_tail"]["alert_count"] == 6


def test_tv_2h_alignment_weights_actionable_higher():
    """In-band agreement should outweigh early-regime disagreement."""
    hour = 1_900_000_000.0
    hour = hour - (hour % 3600)
    now = hour + 1800
    # Early DOWN, in-band UP (price rising) — actionable says UP
    alerts = _alerts_at_sso(hour, [
        (60, "DOWN", 100.0),
        (120, "DOWN", 99.0),
        (1000, "UP", 101.0),
        (1200, "UP", 102.0),
        (1500, "UP", 103.0),
    ])
    rev = compute_tv_2h_review(
        alerts=alerts, now=now, lookback_s=7200, symbol="BTCUSD", oracle_price_now=104.0)
    up_score = tv_2h_alignment_score(rev, "up")
    down_score = tv_2h_alignment_score(rev, "down")
    assert up_score is not None and down_score is not None
    assert up_score > down_score
    segs = segment_alignment_scores(rev, "up")
    assert segs is not None
    assert segs["actionable_trend"]["weight"] == 0.50
    assert segs["open_regime"]["weight"] == 0.30
    assert segs["lookback_tail"]["weight"] == 0.20


def test_tv_2h_trend_p_up_and_alignment():
    review = {
        "enabled": True,
        "alert_count": 5,
        "trend_direction": "up",
        "confidence": 0.8,
        "aligned": True,
    }
    p_up = tv_2h_trend_p_up(review)
    assert p_up is not None
    assert p_up > 0.5
    align = tv_2h_alignment_score(review, "up")
    assert align is not None
    assert align > 0.6
    mis = tv_2h_alignment_score(review, "down")
    assert mis is not None
    assert mis < 0.5


def test_tv_2h_trend_p_up_prefers_actionable_segment():
    review = {
        "enabled": True,
        "alert_count": 6,
        "trend_direction": "down",  # overall down
        "confidence": 0.5,
        "aligned": False,
        "segments": {
            "actionable_trend": {
                "alert_count": 4,
                "trend_direction": "up",
                "confidence": 0.8,
                "aligned": True,
            },
        },
    }
    p = tv_2h_trend_p_up(review)
    assert p is not None
    assert p > 0.5  # follows actionable UP, not overall DOWN


def test_tv_2h_trend_p_up_insufficient_alerts():
    assert tv_2h_trend_p_up({"enabled": True, "alert_count": 1}) is None


def test_tv_2h_trend_p_up_down_trend():
    review = {
        "enabled": True,
        "alert_count": 4,
        "trend_direction": "down",
        "confidence": 0.7,
        "aligned": True,
    }
    p = tv_2h_trend_p_up(review)
    assert p is not None
    assert p < 0.5


def test_tv_ladder_alignment_up_side():
    views = {"tv_5m": 0.7, "tv_15m": 0.65, "tv_60m": 0.6}
    assert tv_ladder_alignment(views, "up") > 0.5
    assert tv_ladder_alignment(views, "down") < 0.5


def test_compute_tv_2h_review_by_timeframe():
    now = 3_700_000.0
    hour = (now // 3600) * 3600
    alerts = _alerts_at_sso(hour, [
        (600, "UP", 100.0),
        (1200, "UP", 101.0),
    ])
    for i, a in enumerate(alerts):
        a["timeframe"] = "15" if i == 0 else "30"
    review = compute_tv_2h_review(alerts=alerts, now=now, symbol="BTCUSD")
    assert "5" not in review.get("by_timeframe", {})
    assert review["by_timeframe"]["15"]["last_direction"] == "UP"
