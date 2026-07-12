"""Tests for directional trade label helpers."""
from __future__ import annotations

from engine.pulse.directional_labels import (
    directional_trade_labels,
    labels_from_research,
    market_tf_from_window,
)


def test_hourly_btc_labels():
    out = directional_trade_labels(
        title="Bitcoin Up or Down - July 7, 3AM ET",
        series_label="btc_1h",
        window_seconds=3600,
    )
    assert out["trade_symbol"] == "BTC"
    assert out["market_tf"] == "1h"


def test_legacy_15m_pulse_is_btc():
    out = directional_trade_labels(series_label="15m", window_seconds=900)
    assert out["trade_symbol"] == "BTC"
    assert out["market_tf"] == "15m"


def test_eth_above_strike():
    out = directional_trade_labels(
        title="Ethereum above 3500 on July 7?",
        series_label="eth_above",
        window_seconds=3600,
        market_kind="above",
    )
    assert out["trade_symbol"] == "ETH"
    assert out["market_tf"] == "1h"
    assert out["market_kind_label"] == "above strike"


def test_labels_from_research_backfill():
    out = labels_from_research(
        {"market_series": "15m", "window_seconds": 900, "series_label": "15m"},
        title="",
    )
    assert out["trade_symbol"] == "BTC"
    assert out["market_tf"] == "15m"


def test_market_tf_from_window_seconds():
    assert market_tf_from_window(window_seconds=3600) == "1h"
    assert market_tf_from_window(series_label="5m") == "5m"
