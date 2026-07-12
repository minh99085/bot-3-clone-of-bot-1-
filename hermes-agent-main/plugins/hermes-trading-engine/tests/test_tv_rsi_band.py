"""Tests for RSI 30/70 band heartbeats (separate FIFO from path + divergence)."""

from __future__ import annotations

import json
from pathlib import Path

from engine.pulse.tradingview import TradingViewIntake
from engine.pulse.tv_rsi_band import (
    classify_rsi_zone,
    filter_rsi_band,
    lean_from_zone,
    resolve_rsi_band_from_intake,
    rsi_band_snapshot,
    summarize_band_history,
)

SECRET = "test-secret-band"
NOW = 1_800_000_000.0


def _band(*, symbol="BTCUSDT", rsi=55.0, zone="neutral", band_event="none",
          bar_time=None, i=0):
    if bar_time is None:
        bar_time = NOW + i * 300.0
    direction = "UP" if zone == "oversold" else ("DOWN" if zone == "overbought" else "FLAT")
    level = ("RSI_OVERSOLD" if zone == "oversold"
             else ("RSI_OVERBOUGHT" if zone == "overbought" else "RSI_NEUTRAL"))
    return {
        "secret": SECRET, "bot_name": "hermes", "symbol": symbol, "timeframe": "5",
        "direction": direction, "signal_level": level, "strength": 0.65,
        "indicator_name": "Hermes RSI Divergence Indicator",
        "signal_kind": "rsi_band",
        "rsi": rsi, "rsi_zone": zone, "band_event": band_event,
        "rsi_os_threshold": 30, "rsi_ob_threshold": 70,
        "event_id": "%s-5-%d-RSI_BAND-bot1" % (symbol, i),
        "bar_time": str(int(bar_time * 1000)),
        "price": 100.0, "observe_only": True,
    }


def test_classify_rsi_zone():
    assert classify_rsi_zone(25) == "oversold"
    assert classify_rsi_zone(30) == "oversold"
    assert classify_rsi_zone(50) == "neutral"
    assert classify_rsi_zone(70) == "overbought"
    assert classify_rsi_zone(80) == "overbought"
    assert classify_rsi_zone(None) is None


def test_lean_from_zone():
    assert lean_from_zone("oversold") == "up"
    assert lean_from_zone("overbought") == "down"
    assert lean_from_zone("neutral") is None


def test_rsi_band_fifo_separate(tmp_path: Path):
    intake = TradingViewIntake(
        secret=SECRET, allowed_symbols=("BTCUSDT",), data_dir=str(tmp_path),
        alert_history_per_symbol=50, rsi_div_history_per_symbol=20,
        rsi_band_history_per_symbol=50, max_age_s=1e9)
    for i in range(5):
        code, body = intake.ingest(
            json.dumps(_band(i=i, rsi=40 + i, bar_time=NOW + i * 300)).encode(),
            now=NOW + i * 300 + 60)
        assert code == 200 and body.get("ok"), body
    band = intake.rsi_band_history_for_symbol("BTCUSDT")
    path = intake.alert_history_for_symbol("BTCUSDT")
    assert len(band) == 5
    assert len(path) == 0
    assert all(r.get("signal_kind") == "rsi_band" for r in band)


def test_rsi_band_snapshot_and_summary():
    rows = [
        _band(i=0, rsi=72, zone="overbought", band_event="enter_overbought"),
        _band(i=1, rsi=68, zone="neutral", band_event="exit_overbought"),
        _band(i=2, rsi=28, zone="oversold", band_event="enter_oversold"),
    ]
    snap = rsi_band_snapshot(rows, now=NOW + 900, max_age_s=900)
    assert snap is not None
    assert snap["rsi_zone"] == "oversold"
    assert snap["lean"] == "up"
    assert snap["band_event"] == "enter_oversold"
    summary = summarize_band_history(rows)
    assert summary["n"] == 3
    assert summary["oversold_bars"] == 1
    assert summary["overbought_bars"] == 1
    assert "enter_oversold" in summary["recent_crosses"]


def test_resolve_strict_lane_no_cross_feed(tmp_path: Path):
    """15m lane: BTCUSD query reads INDEX FIFO only — never falls back to USDT."""
    intake = TradingViewIntake(
        secret=SECRET, allowed_symbols=("BTCUSD", "BTCUSDT"), data_dir=str(tmp_path),
        rsi_band_history_per_symbol=50, max_age_s=1e9)
    intake.ingest(json.dumps(_band(symbol="BTCUSD", i=0, rsi=75, zone="overbought",
                                  bar_time=NOW - 60)).encode(), now=NOW)
    intake.ingest(json.dumps(_band(symbol="BTCUSDT", i=1, rsi=25, zone="oversold",
                                  bar_time=NOW - 30)).encode(), now=NOW)
    snap = resolve_rsi_band_from_intake(intake, "BTCUSD", now=NOW, max_age_s=900)
    assert snap is not None
    assert snap["resolved_symbol"] == "BTCUSD"
    assert snap["rsi_zone"] == "overbought"
    assert snap["lean"] == "down"


def test_filter_rsi_band():
    mixed = [_band(i=0), {"signal_kind": "bar_close_5m"}, {"signal_kind": "rsi_divergence"}]
    assert len(filter_rsi_band(mixed)) == 1
