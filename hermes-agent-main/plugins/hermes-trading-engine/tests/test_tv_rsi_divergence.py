"""Tests for RSI divergence analysis (primer + history for Grok)."""

from __future__ import annotations

import json
from pathlib import Path

from engine.pulse.tradingview import TradingViewIntake
from engine.pulse.tv_rsi_divergence import (
    RSI_DIVERGENCE_PRIMER,
    classify_divergence,
    resolve_rsi_divergence_from_intake,
    rsi_divergence_snapshot,
    summarize_divergence_history,
)

SECRET = "test-secret-div"
NOW = 1_800_000_000.0


def _rsi_div(*, symbol="BTCUSDT", direction="UP", rsi=32.0, i=0, bar_time=None):
    if bar_time is None:
        bar_time = NOW + i * 300.0
    level = "REGULAR_BULL_DIV" if direction == "UP" else "REGULAR_BEAR_DIV"
    kind = "regular_bullish" if direction == "UP" else "regular_bearish"
    return {
        "secret": SECRET, "bot_name": "hermes", "symbol": symbol, "timeframe": "5",
        "direction": direction, "signal_level": level, "strength": 0.75,
        "indicator_name": "Hermes RSI Divergence Indicator",
        "signal_kind": "rsi_divergence", "divergence_kind": kind,
        "event_id": "%s-5-%d-%s-rsidiv-bot1" % (symbol, i, level),
        "bar_time": str(int(bar_time * 1000)),
        "price": 100.0, "rsi": rsi, "observe_only": True,
    }


def test_primer_has_bull_bear_mechanics():
    assert "regular_bullish" in RSI_DIVERGENCE_PRIMER
    assert "lower low" in RSI_DIVERGENCE_PRIMER["regular_bullish"]["pattern"]
    assert "higher low" in RSI_DIVERGENCE_PRIMER["regular_bullish"]["pattern"]
    assert RSI_DIVERGENCE_PRIMER["regular_bearish"]["lean"] == "down"
    tv = RSI_DIVERGENCE_PRIMER["tradingview_official"]
    assert "tradingview.com" in tv["source_url"]
    assert tv["wilder_oversold"] == 30
    assert tv["wilder_overbought"] == 70
    ind = RSI_DIVERGENCE_PRIMER["operator_indicator"]
    assert "BTCUSDT" in ind["chart_symbols"]
    assert "BTCUSD" in ind["chart_symbols"]
    assert ind["config"]["rsi_period"] == 14
    assert ind["config"]["pivot_lookback_left"] == 5
    assert "hidden_bullish" in RSI_DIVERGENCE_PRIMER
    assert RSI_DIVERGENCE_PRIMER["hidden_bullish"]["webhook_to_bot"] is False


def test_classify_regular_bull():
    info = classify_divergence(_rsi_div(direction="UP", rsi=28))
    assert info["divergence_type"] == "regular_bullish"
    assert info["lean"] == "up"
    assert info["rsi_zone_at_signal"] == "oversold"
    assert "weakening" in (info["meaning"] or "").lower()


def test_classify_regular_bear():
    info = classify_divergence(_rsi_div(direction="DOWN", rsi=68))
    assert info["divergence_type"] == "regular_bearish"
    assert info["lean"] == "down"


def test_divergence_snapshot_with_history():
    rows = [
        _rsi_div(i=0, direction="DOWN", rsi=72),
        _rsi_div(i=1, direction="UP", rsi=30),
    ]
    snap = rsi_divergence_snapshot(rows, now=NOW + 400, max_age_s=2700)
    assert snap["has_signal"] is True
    assert snap["primer"] == RSI_DIVERGENCE_PRIMER
    assert snap["latest"]["divergence_type"] == "regular_bullish"
    assert snap["confirm_fade_by_side"]["up"]["decision"] == "confirm"
    assert snap["confirm_fade_by_side"]["down"]["decision"] == "fade"
    summary = summarize_divergence_history(rows)
    assert summary["n"] == 2
    assert summary["bull_count"] == 1
    assert summary["bear_count"] == 1


def test_empty_fifo_still_returns_primer():
    snap = rsi_divergence_snapshot([], now=NOW)
    assert snap["has_signal"] is False
    assert snap["primer"] == RSI_DIVERGENCE_PRIMER


def test_resolve_from_intake(tmp_path: Path):
    intake = TradingViewIntake(
        secret=SECRET, allowed_symbols=("BTCUSDT",), data_dir=str(tmp_path),
        rsi_div_history_per_symbol=20, max_age_s=1e9)
    intake.ingest(json.dumps(_rsi_div(i=0, direction="UP")).encode(), now=NOW + 60)
    snap = resolve_rsi_divergence_from_intake(intake, "BTCUSDT", now=NOW + 120)
    assert snap is not None
    assert snap["resolved_symbol"] == "BTCUSDT"
    assert snap["has_signal"] is True
    assert snap["latest"]["lean"] == "up"
