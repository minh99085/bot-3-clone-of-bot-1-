"""Tests for Hermes BarClose 15m dual-horizon price path (50 regime + 6–8 short)."""

from __future__ import annotations

import json
from pathlib import Path

from engine.pulse.grok_bundle import grok_task_for_window, order_bundle_for_grok
from engine.pulse.tradingview import TradingViewIntake
from engine.pulse.tv_15m_price_path import (
    build_price_path,
    dual_horizon_price_path,
    filter_bar_close_15m,
    hourly_chart_lean_entry_ok,
    price_path_trend,
    size_mult_for_lean,
    trade_lean_from_path,
    tv_15m_price_path_snapshot,
)


SECRET = "test-secret-bar15m"
NOW = 1_800_000_000.0  # unix seconds


def _bar_payload(*, symbol="BTCUSD", direction="UP", price=100.0, bar_time=None,
                 event_id="BTCUSD-15-1-BAR_BULL-bar15m-bot1", streak=1):
    if bar_time is None:
        bar_time = NOW
    return {
        "secret": SECRET,
        "bot_name": "hermes",
        "symbol": symbol,
        "timeframe": "15",
        "direction": direction,
        "signal_level": "BAR_BULL" if direction == "UP" else "BAR_BEAR",
        "strength": 0.8,
        "indicator_name": "Hermes BarClose 15m",
        "signal_kind": "bar_close_15m",
        "event_id": event_id,
        "bar_time": str(int(bar_time * 1000)),  # TradingView ms
        "price": price,
        "open": price - 1.0 if direction == "UP" else price + 1.0,
        "high": price + 2.0,
        "low": price - 2.0,
        "close": price,
        "body_pct": 0.5 if direction == "UP" else -0.5,
        "body_ratio": 0.6,
        "streak": streak,
        "bar_confirmed": True,
        "non_repainting": True,
        "observe_only": True,
    }


def test_fifo_hard_cap_50(tmp_path: Path):
    intake = TradingViewIntake(
        secret=SECRET, allowed_symbols=("BTCUSD", "ETHUSD"),
        data_dir=str(tmp_path), alert_history_per_symbol=50, max_age_s=1e9)
    for i in range(55):
        payload = _bar_payload(
            price=100.0 + i,
            event_id="BTCUSD-15-%d-BAR_BULL-bar15m-bot1" % i,
            bar_time=NOW + i * 900.0,
            streak=i + 1)
        code, body = intake.ingest(json.dumps(payload).encode("utf-8"), now=NOW + 55 * 900.0)
        assert code == 200 and body.get("ok") is True, body
    hist = intake.alert_history_for_symbol("BTCUSD")
    assert len(hist) == 50
    assert hist[0]["price"] == 105.0
    assert hist[-1]["price"] == 154.0
    assert hist[0].get("signal_kind") == "bar_close_15m"
    assert hist[0].get("open") is not None
    assert hist[0].get("close") == 105.0


def test_compact_alert_keeps_ohlc():
    intake = TradingViewIntake(
        secret=SECRET, allowed_symbols=("BTCUSD",), data_dir=None,
        alert_history_per_symbol=50, max_age_s=1e9)
    payload = _bar_payload()
    code, body = intake.ingest(json.dumps(payload).encode("utf-8"), now=NOW)
    assert code == 200, body
    row = intake.alert_history_for_symbol("BTCUSD")[0]
    assert row["direction"] == "UP"
    assert row["open"] == 99.0
    assert row["close"] == 100.0
    assert row["signal_kind"] == "bar_close_15m"


def test_price_path_trend_from_last_50():
    alerts = []
    for i in range(8):
        alerts.append({
            "direction": "DOWN",
            "price": 2000 - i * 10,
            "close": 2000 - i * 10,
            "open": 2000 - i * 10 + 5,
            "high": 2005 - i * 10,
            "low": 1990 - i * 10,
            "signal_kind": "bar_close_15m",
            "signal_level": "BAR_BEAR",
            "timeframe": "15",
            "bar_time": 1000 + i,
        })
    alerts.append({"direction": "UP", "price": 999, "timeframe": "15",
                   "signal_level": "REGULAR_BULL_DIV"})
    filtered = filter_bar_close_15m(alerts)
    assert all(a.get("signal_kind") == "bar_close_15m" for a in filtered)
    path = build_price_path(filtered, max_points=50)
    assert len(path) == 8
    trend = price_path_trend(alerts, max_points=50)
    assert trend["n"] == 8
    assert trend["trend"]["pattern"] == "downtrend"
    assert trend["price_delta_pct"] is not None
    assert trend["price_delta_pct"] < 0


def test_dual_horizon_short_vs_regime():
    # Long downtrend regime, then short-term recovery (last 8 up)
    alerts = []
    for i in range(40):
        alerts.append({
            "direction": "DOWN", "price": 2000 - i, "close": 2000 - i,
            "signal_kind": "bar_close_15m", "signal_level": "BAR_BEAR", "timeframe": "15",
        })
    for i in range(8):
        alerts.append({
            "direction": "UP", "price": 1960 + i, "close": 1960 + i,
            "signal_kind": "bar_close_15m", "signal_level": "BAR_BULL", "timeframe": "15",
        })
    dual = dual_horizon_price_path(alerts, regime_n=50, short_n=8)
    assert dual["short_term"]["n"] == 8
    assert dual["short_term"]["lean"] == "up"
    assert dual["regime"]["n"] == 48
    assert dual["regime"]["lean"] == "down" or dual["regime"]["trend"]["pattern"] in (
        "downtrend", "downtrend_bias", "mixed", "choppy")
    assert dual["alignment"] in ("divergent", "short_only", "aligned")
    # Short drives trade lean even when regime disagrees
    assert dual["trade_lean"] == "up"
    lean = trade_lean_from_path(dual)
    assert lean["trade_lean"] == "up"
    assert lean["short_n"] == 8


def test_size_mult_for_lean_aligned_vs_oppose():
    aligned = {"trade_lean": "down", "alignment": "aligned"}
    assert size_mult_for_lean(side="down", lean=aligned) > 1.0
    assert size_mult_for_lean(side="up", lean=aligned) < 0.5
    none = {"trade_lean": None, "alignment": "none"}
    assert size_mult_for_lean(side="up", lean=none) == 1.0


def test_tv_15m_price_path_snapshot_dual():
    history = {
        "by_symbol": {
            "BTCUSD": [
                {"direction": "UP", "price": 100 + i, "close": 100 + i,
                 "signal_kind": "bar_close_15m"} for i in range(12)
            ],
            "ETHUSD": [
                {"direction": "DOWN", "price": 3000 - i, "close": 3000 - i,
                 "signal_kind": "bar_close_15m"} for i in range(12)
            ],
        }
    }
    snap = tv_15m_price_path_snapshot(
        history=history, focus_symbol="ETHUSD", max_points=50, short_n=8)
    assert snap["short_n"] == 8
    assert snap["focus"]["short_term"]["n"] == 8
    assert snap["focus"]["short_term"]["lean"] == "down"
    assert snap["focus"]["trade_lean"] == "down"
    assert snap["by_symbol"]["BTCUSD"]["short_term"]["lean"] == "up"


def test_grok_15m_task_points_at_dual_horizon():
    task = grok_task_for_window(series_label="btc_15m", window_seconds=900, ttc_s=300.0)
    assert "short_path" in task["tv_role"]
    assert "BTCUSD" in task["tv_role"]
    assert "tradingview_alert_interpretation" in task["tv_role"]
    assert "1_tradingview_alert_interpretation" in task["decision_priority"]
    assert "3_tv_5m_bar_close_short_path_pattern" in task["decision_priority"]
    assert task["in_entry_band"] is True


def test_bundle_priority_includes_15m_price_path():
    b = order_bundle_for_grok({
        "tradingview_15m_price_path": {"focus": {}},
        "tradingview_alert_history": {"focus": {}},
        "lessons": [1],
    })
    keys = list(b.keys())
    assert keys.index("tradingview_15m_price_path") < keys.index("lessons")


def test_load_state_respects_hard_cap_50(tmp_path: Path):
    path = tmp_path / "btc_pulse_tradingview.json"
    rows = [{"event_id": "e%d" % i, "direction": "UP", "price": float(i),
             "received_at": float(i), "signal_kind": "bar_close_15m"} for i in range(80)]
    path.write_text(json.dumps({
        "received": 80, "valid": 80, "rejected": 0, "consumed": 0,
        "reject_reasons": {}, "seen_ids": [],
        "alert_history_per_symbol": 120,
        "alert_history_by_symbol": {"BTCUSD": rows},
    }), encoding="utf-8")
    intake = TradingViewIntake(
        secret=SECRET, allowed_symbols=("BTCUSD",), data_dir=str(tmp_path),
        alert_history_per_symbol=50, max_age_s=1e9)
    hist = intake.alert_history_for_symbol("BTCUSD")
    assert len(hist) == 50
    assert hist[0]["price"] == 30.0
    assert hist[-1]["price"] == 79.0


def test_hourly_chart_lean_gate_blocks_early_and_opposed():
    rows = []
    for i in range(6):
        rows.append(_bar_payload(
            direction="DOWN",
            price=100.0 - i,
            event_id="BTCUSD-15-%d-BAR_BEAR-bar15m-bot1" % i,
            bar_time=NOW + i * 900.0,
            streak=i + 1))
    dual = dual_horizon_price_path(rows, regime_n=50, short_n=6)
    lean = trade_lean_from_path(dual)
    assert lean["short_n"] == 6
    assert lean["trade_lean"] == "down"
    ok, reason = hourly_chart_lean_entry_ok(
        side="up", lean=lean, seconds_since_open=1200.0, min_short_n=6, min_sso_s=900.0)
    assert ok is False and reason == "hourly_chart_lean_opposed"
    ok2, reason2 = hourly_chart_lean_entry_ok(
        side="down", lean=lean, seconds_since_open=1200.0, min_short_n=6, min_sso_s=900.0)
    assert ok2 is True and reason2 == "ok"
    ok3, reason3 = hourly_chart_lean_entry_ok(
        side="down", lean=lean, seconds_since_open=600.0, min_short_n=6, min_sso_s=900.0)
    assert ok3 is False and reason3 == "hourly_chart_lean_too_early"
    ok4, reason4 = hourly_chart_lean_entry_ok(
        side="down", lean={"short_n": 3, "trade_lean": "down"},
        seconds_since_open=1200.0, min_short_n=6, min_sso_s=900.0)
    assert ok4 is False and reason4 == "hourly_chart_lean_cold"
