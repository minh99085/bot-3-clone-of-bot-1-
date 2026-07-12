"""Tests for unified TV alert interpretation (Grok/bot training)."""

from __future__ import annotations

from engine.pulse.tv_alert_interpretation import (
    TV_ALERT_GUIDE,
    interpret_tv_for_window,
    tv_grok_reading_guide,
)


def test_tv_alert_guide_has_official_sources():
    urls = [s["url"] for s in TV_ALERT_GUIDE["sources"]]
    assert any("43000502338" in u for u in urls)  # Wilder RSI
    assert any("43000589127" in u for u in urls)  # divergence indicator
    assert "bar_close_5m" in TV_ALERT_GUIDE["signal_kinds"]
    assert "cardwell_trend_context" in TV_ALERT_GUIDE


def test_grok_reading_guide_lane_symbols_1h():
    role = tv_grok_reading_guide(window_seconds=3600, series_label="btc_1h")
    assert "BTCUSDT" in role
    assert "ETHUSDT" in role
    assert "tradingview_alert_interpretation" in role


def test_grok_reading_guide_lane_symbols_15m():
    role = tv_grok_reading_guide(window_seconds=900, series_label="btc_15m")
    assert "BTCUSD" in role
    assert "ETHUSD" in role
    assert "Chainlink" in role or "INDEX" in role


def test_interpret_tv_bullish_consensus():
    out = interpret_tv_for_window(
        window_seconds=900,
        series_label="btc_15m",
        tv_chart_lane={"lane": "15m", "chart_symbol": "BTCUSD", "feed": "chainlink_index_usd"},
        price_path={
            "price_pattern": {"trade_lean": "up", "alignment": "aligned", "confidence": "high"},
        },
        rsi_band={"lean": "up", "rsi": 28.0, "rsi_zone": "oversold"},
        rsi_divergence={
            "has_signal": True,
            "latest": {"lean": "up", "divergence_type": "regular_bullish"},
            "confirm_fade_by_side": {"up": "confirm", "down": "fade"},
        },
    )
    assert out["composite_lean"] == "up"
    assert out["signal_agreement"]["agreement"] == "bullish_consensus"
    assert out["signal_agreement"]["confidence"] == "high"
    assert out["lane"] == "15m"
    assert out["chart_symbol"] == "BTCUSD"


def test_interpret_tv_conflicted_prefers_path():
    out = interpret_tv_for_window(
        window_seconds=900,
        price_path={"price_pattern": {"trade_lean": "up", "alignment": "divergent"}},
        rsi_band={"lean": "down", "rsi_zone": "overbought"},
        rsi_divergence={"has_signal": False},
    )
    assert out["signal_agreement"]["agreement"] == "conflicted"
    assert out["composite_lean"] == "up"
    assert out["cardwell_hint"] is None


def test_interpret_tv_cardwell_hint_on_divergence_opposes_path():
    out = interpret_tv_for_window(
        window_seconds=900,
        price_path={"price_pattern": {"trade_lean": "up"}},
        rsi_divergence={
            "has_signal": True,
            "latest": {"lean": "down", "divergence_type": "regular_bearish"},
        },
    )
    assert "Cardwell" in (out.get("cardwell_hint") or "")
    assert "correction" in (out.get("cardwell_hint") or "").lower()


def test_interpret_tv_confirm_fade_for_side():
    out = interpret_tv_for_window(
        window_seconds=900,
        price_path={"price_pattern": {"trade_lean": "up"}},
        rsi_divergence={
            "has_signal": True,
            "latest": {"lean": "up"},
            "confirm_fade_by_side": {"up": "confirm", "down": "fade"},
        },
        trade_side="up",
    )
    assert out["confirm_fade"]["path_aligned"] is True
    assert out["confirm_fade"]["divergence_overlay"] == "confirm"
