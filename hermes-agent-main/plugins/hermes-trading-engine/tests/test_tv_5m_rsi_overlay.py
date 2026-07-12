"""Tests for 5m bar-close path + RSI overlay (split FIFOs)."""

from __future__ import annotations

import json
from pathlib import Path

from engine.pulse.tradingview import TradingViewIntake
from engine.pulse.tv_15m_price_path import dual_horizon_price_path, filter_bar_close
from engine.pulse.tv_rsi_overlay import (
    filter_rsi_divergence,
    latest_rsi_overlay,
    size_mult_for_rsi_overlay,
)

SECRET = "test-secret-5m"
NOW = 1_800_000_000.0


def _bar5(*, symbol="BTCUSD", direction="UP", price=100.0, bar_time=None, i=0):
    if bar_time is None:
        bar_time = NOW + i * 300.0
    return {
        "secret": SECRET, "bot_name": "hermes", "symbol": symbol, "timeframe": "5",
        "direction": direction,
        "signal_level": "BAR_BULL" if direction == "UP" else "BAR_BEAR",
        "strength": 0.8, "indicator_name": "Hermes BarClose 5m",
        "signal_kind": "bar_close_5m",
        "event_id": "%s-5-%d-%s-bar5m-bot1" % (symbol, i, "BAR_BULL" if direction == "UP" else "BAR_BEAR"),
        "bar_time": str(int(bar_time * 1000)),
        "price": price, "open": price - 1, "high": price + 1, "low": price - 2,
        "close": price, "body_pct": 0.5, "body_ratio": 0.6, "streak": 1,
        "observe_only": True,
    }


def _rsi(*, symbol="BTCUSD", direction="UP", price=100.0, bar_time=None, i=0):
    if bar_time is None:
        bar_time = NOW + i * 300.0
    return {
        "secret": SECRET, "bot_name": "hermes", "symbol": symbol, "timeframe": "5",
        "direction": direction,
        "signal_level": "REGULAR_BULL_DIV" if direction == "UP" else "REGULAR_BEAR_DIV",
        "strength": 0.85, "indicator_name": "Hermes RSI Divergence 5m Accurate",
        "signal_kind": "rsi_divergence",
        "divergence_kind": "regular_bullish" if direction == "UP" else "regular_bearish",
        "event_id": "%s-5-%d-RSI-rsidiv5m-bot1" % (symbol, i),
        "bar_time": str(int(bar_time * 1000)),
        "price": price, "rsi": 32.0 if direction == "UP" else 68.0,
        "rsi_delta": 5.0, "price_delta_pct": 0.2, "observe_only": True,
    }


def test_split_fifo_bar_vs_rsi(tmp_path: Path):
    intake = TradingViewIntake(
        secret=SECRET, allowed_symbols=("BTCUSD",), data_dir=str(tmp_path),
        alert_history_per_symbol=50, rsi_div_history_per_symbol=20, max_age_s=1e9)
    for i in range(10):
        code, body = intake.ingest(json.dumps(_bar5(i=i, price=100 + i)).encode(), now=NOW + 20 * 300)
        assert code == 200 and body.get("ok")
    for i in range(3):
        code, body = intake.ingest(
            json.dumps(_rsi(i=i, bar_time=NOW + (10 + i) * 300)).encode(),
            now=NOW + 20 * 300)
        assert code == 200 and body.get("ok"), body
    path = intake.alert_history_for_symbol("BTCUSD")
    rsi = intake.rsi_div_history_for_symbol("BTCUSD")
    assert len(path) == 10
    assert all(r.get("signal_kind") == "bar_close_5m" for r in path)
    assert len(rsi) == 3
    assert all(r.get("signal_kind") == "rsi_divergence" for r in rsi)
    assert filter_bar_close(path + rsi) == path
    assert len(filter_rsi_divergence(path + rsi)) == 3


def test_5m_path_prefers_bar_close_5m():
    rows = [_bar5(i=i, direction="DOWN" if i < 4 else "UP", price=100 - i) for i in range(8)]
    dual = dual_horizon_price_path(rows, regime_n=50, short_n=8)
    assert dual["short_term"]["n"] == 8
    assert dual["trade_lean"] in ("up", "down", None)


def test_rsi_overlay_size_mult():
    ov = latest_rsi_overlay([_rsi(direction="DOWN")], now=NOW + 60, max_age_s=2700)
    assert ov and ov["lean"] == "down"
    assert size_mult_for_rsi_overlay(side="down", overlay=ov) == 1.15
    assert size_mult_for_rsi_overlay(side="up", overlay=ov) == 0.45
    assert size_mult_for_rsi_overlay(side="up", overlay=None) == 1.0


def test_strict_lane_keeps_index_usd_on_15m():
    from engine.pulse.tv_15m_price_path import (
        compact_path_for_plot, resolve_bar_close_history, tv_15m_price_path_snapshot)

    by = {
        "BTCUSD": [
            {"signal_kind": "bar_close_5m", "signal_level": "BAR_BULL", "direction": "UP",
             "timeframe": "5", "close": 100.0, "price": 100.0, "open": 99, "high": 101,
             "low": 98, "received_at": NOW, "bar_time": NOW},
        ],
        "BTCUSDT": [
            {"signal_kind": "bar_close_5m", "signal_level": "BAR_BULL", "direction": "UP",
             "timeframe": "5", "close": 110.0 + i, "price": 110.0 + i, "open": 109 + i,
             "high": 111 + i, "low": 108 + i, "received_at": NOW + i * 300,
             "bar_time": NOW + i * 300}
            for i in range(10)
        ],
    }
    sym, rows = resolve_bar_close_history(by, "BTCUSD", strict_lane=True)
    assert sym == "BTCUSD"
    assert len(rows) == 1
    sym1h, rows1h = resolve_bar_close_history(by, "BTCUSDT", strict_lane=True)
    assert sym1h == "BTCUSDT"
    assert len(rows1h) == 10
    snap = tv_15m_price_path_snapshot(history={"by_symbol": by}, focus_symbol="BTCUSD",
                                      max_points=50, short_n=8)
    assert snap["focus_symbol"] == "BTCUSD"
    assert snap["price_pattern"]["short_path"]
    assert compact_path_for_plot(snap["focus"])["short_path"]


def test_legacy_cross_feed_when_strict_disabled():
    from engine.pulse.tv_15m_price_path import resolve_bar_close_history

    by = {
        "BTCUSD": [
            {"signal_kind": "bar_close_15m", "signal_level": "BAR_BEAR", "direction": "DOWN",
             "timeframe": "15", "close": 100.0, "price": 100.0, "open": 101, "high": 102,
             "low": 99, "received_at": NOW - 3600, "bar_time": NOW - 3600},
        ],
        "BTCUSDT": [
            {"signal_kind": "bar_close_5m", "signal_level": "BAR_BULL", "direction": "UP",
             "timeframe": "5", "close": 110.0 + i, "price": 110.0 + i, "open": 109 + i,
             "high": 111 + i, "low": 108 + i, "received_at": NOW + i * 300,
             "bar_time": NOW + i * 300}
            for i in range(10)
        ],
    }
    sym, rows = resolve_bar_close_history(by, "BTCUSD", strict_lane=False)
    assert sym == "BTCUSDT"
    assert len(rows) == 10


def test_rsi_resolve_strict_lane_symbol(tmp_path: Path):
    from engine.pulse.tv_rsi_overlay import resolve_rsi_overlay_from_intake
    intake = TradingViewIntake(
        secret=SECRET, allowed_symbols=("BTCUSD", "BTCUSDT"), data_dir=str(tmp_path),
        alert_history_per_symbol=50, rsi_div_history_per_symbol=20, max_age_s=1e9)
    intake.ingest(json.dumps(_rsi(symbol="BTCUSD", i=0, bar_time=NOW - 60)).encode(),
                  now=NOW)
    intake.ingest(json.dumps(_rsi(symbol="BTCUSDT", i=1, bar_time=NOW - 10)).encode(),
                  now=NOW)
    ov15 = resolve_rsi_overlay_from_intake(intake, "BTCUSD", now=NOW, max_age_s=2700)
    assert ov15 is not None
    assert ov15["resolved_symbol"] == "BTCUSD"
    ov1h = resolve_rsi_overlay_from_intake(intake, "BTCUSDT", now=NOW, max_age_s=2700)
    assert ov1h is not None
    assert ov1h["resolved_symbol"] == "BTCUSDT"
    assert ov1h["lean"] == "up"
