"""Lane-aware TV chart routing: 1h -> *USDT, 15m -> INDEX *USD."""

from __future__ import annotations

import json

from engine.pulse.tradingview import (
    tv_chart_symbol_for_window,
    tv_lane_kind,
    tv_symbol_for_series_slug,
    tv_symbol_for_window,
)
from engine.pulse.tv_15m_price_path import resolve_bar_close_history


class _Win:
    def __init__(self, *, series_slug: str, window_seconds: int, series_label: str = ""):
        self.series_slug = series_slug
        self.window_seconds = window_seconds
        self.series_label = series_label


def test_lane_kind_hourly_vs_15m():
    assert tv_lane_kind(window_seconds=3600, series_slug="btc-up-or-down-hourly") == "1h"
    assert tv_lane_kind(window_seconds=900, series_slug="btc-up-or-down-15m") == "15m"
    assert tv_lane_kind(window_seconds=900, series_slug="eth-up-or-down-15m") == "15m"


def test_chart_symbol_btc_hourly_usdt():
    w = _Win(series_slug="btc-up-or-down-hourly", window_seconds=3600, series_label="btc_1h")
    assert tv_symbol_for_window(w) == "BTCUSDT"
    assert tv_chart_symbol_for_window(w) == "BTCUSDT"


def test_chart_symbol_btc_15m_index_usd():
    w = _Win(series_slug="btc-up-or-down-15m", window_seconds=900, series_label="btc_15m")
    assert tv_symbol_for_window(w) == "BTCUSD"
    assert tv_chart_symbol_for_window(w) == "BTCUSD"


def test_chart_symbol_eth_hourly_usdt():
    w = _Win(series_slug="eth-up-or-down-hourly", window_seconds=3600, series_label="eth_1h")
    assert tv_symbol_for_window(w) == "ETHUSDT"


def test_chart_symbol_eth_15m_index_usd():
    w = _Win(series_slug="eth-up-or-down-15m", window_seconds=900, series_label="eth_15m")
    assert tv_symbol_for_window(w) == "ETHUSD"


def test_series_slug_infers_lane_without_window_object():
    assert tv_symbol_for_series_slug("btc-up-or-down-hourly") == "BTCUSDT"
    assert tv_symbol_for_series_slug("btc-up-or-down-15m") == "BTCUSD"
    assert tv_symbol_for_series_slug("eth-up-or-down-hourly") == "ETHUSDT"
    assert tv_symbol_for_series_slug("eth-up-or-down-15m") == "ETHUSD"


def test_bar_close_strict_lane_no_cross_feed():
    """15m lane must not read BTCUSDT bar-close when BTCUSD is requested."""
    hist = {
        "BTCUSDT": [{"signal_kind": "bar_close_5m", "direction": "UP", "price": 1.0,
                     "received_at": 100.0}],
        "BTCUSD": [{"signal_kind": "bar_close_5m", "direction": "DOWN", "price": 2.0,
                    "received_at": 200.0}],
    }
    sym, rows = resolve_bar_close_history(hist, "BTCUSD", strict_lane=True)
    assert sym == "BTCUSD"
    assert rows[0]["direction"] == "DOWN"

    sym1h, rows1h = resolve_bar_close_history(hist, "BTCUSDT", strict_lane=True)
    assert sym1h == "BTCUSDT"
    assert rows1h[0]["direction"] == "UP"


def test_intake_stores_separate_fifo_per_symbol(tmp_path):
    from engine.pulse.tradingview import TradingViewIntake

    intake = TradingViewIntake(
        secret="lane-secret",
        allowed_symbols=frozenset({"BTCUSD", "BTCUSDT"}),
        data_dir=str(tmp_path),
        alert_history_per_symbol=20,
        max_age_s=1e9,
    )
    now = 1_800_000_000.0
    for sym, direction in (("BTCUSD", "DOWN"), ("BTCUSDT", "UP")):
        payload = {
            "secret": "lane-secret",
            "bot_name": "hermes",
            "symbol": sym,
            "timeframe": "5",
            "direction": direction,
            "signal_kind": "bar_close_5m",
            "signal_level": "BAR_BULL" if direction == "UP" else "BAR_BEAR",
            "strength": 0.8,
            "event_id": "%s-5m-1-bar-lane-bot1" % sym,
            "bar_time": str(int(now * 1000)),
            "price": 100.0,
        }
        code, body = intake.ingest(json.dumps(payload).encode(), now=now)
        assert code == 200 and body.get("ok") is True

    w15 = _Win(series_slug="btc-up-or-down-15m", window_seconds=900)
    w1h = _Win(series_slug="btc-up-or-down-hourly", window_seconds=3600)
    from engine.pulse.tv_15m_price_path import resolve_bar_close_from_intake

    sym15, rows15 = resolve_bar_close_from_intake(intake, tv_symbol_for_window(w15))
    sym1h, rows1h = resolve_bar_close_from_intake(intake, tv_symbol_for_window(w1h))
    assert sym15 == "BTCUSD" and rows15[-1]["direction"] == "DOWN"
    assert sym1h == "BTCUSDT" and rows1h[-1]["direction"] == "UP"


def test_usdt_and_eth_symbols_accepted_default_allowlist():
    """Operator lane charts: INDEX *USD (15m) + BINANCE *USDT (1h) must all land."""
    from engine.pulse.tradingview import TradingViewIntake, normalize_symbol

    intake = TradingViewIntake(
        secret="s",
        allowed_symbols=[
            "BTCUSD", "INDEX:BTCUSD", "ETHUSD", "INDEX:ETHUSD",
            "BTCUSDT", "BINANCE:BTCUSDT", "ETHUSDT", "BINANCE:ETHUSDT",
        ],
        bot_name="hermes",
    )
    for raw in ("INDEX:BTCUSD", "INDEX:ETHUSD", "BINANCE:BTCUSDT", "BINANCE:ETHUSDT",
                "BTCUSDT", "ETHUSDT"):
        assert intake._symbol_allowed(normalize_symbol(raw)), raw
    # USDT must NOT collapse into the USD feature symbol (separate 1h-lane FIFO).
    assert intake._storage_symbol("BINANCE:BTCUSDT") == "BTCUSDT"
    assert intake._storage_symbol("BINANCE:ETHUSDT") == "ETHUSDT"
    assert intake._storage_symbol("INDEX:BTCUSD") == "BTCUSD"
