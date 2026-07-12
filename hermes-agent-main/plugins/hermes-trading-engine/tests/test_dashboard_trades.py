"""Tests for directional dashboard trade aggregation and stats."""
from engine.pulse.dashboard_trades import (
    directional_trades_for_dashboard,
    lane_stats,
    lane_trades_for_dashboard,
    recent_trades_for_dashboard,
    symbol_stats,
    symbol_trades_for_dashboard,
)


def test_directional_row_includes_symbol_and_market_tf():
    ledger = {
        "positions": [
            {
                "side": "down",
                "entry_ts": 1500.0,
                "open_ts": 1000.0,
                "entry_price": 0.52,
                "status": "settled",
                "won": True,
                "pnl_usd": 177.36,
                "title": "Bitcoin Up or Down - July 7, 3AM ET",
                "research": {
                    "series_label": "btc_1h",
                    "market_series": "1h",
                    "window_seconds": 3600,
                },
            }
        ],
    }
    rows = directional_trades_for_dashboard(ledger, limit=5)
    assert rows[0]["trade_symbol"] == "BTC"
    assert rows[0]["market_tf"] == "1h"


def test_directional_row_includes_ttm_from_entry_ttc():
    ledger = {
        "positions": [
            {
                "side": "up",
                "entry_ts": 1_000_000.0,
                "close_ts": 1_000_191.0,
                "entry_price": 0.48,
                "status": "settled",
                "won": True,
                "pnl_usd": 12.0,
                "research": {
                    "series_label": "eth_1h",
                    "window_seconds": 3600,
                    "entry_ttc_s": 190.99,
                },
            }
        ],
    }
    rows = directional_trades_for_dashboard(ledger, limit=5)
    assert rows[0]["ttm_s"] == 191.0
    assert rows[0]["ttm_label"] == "3m 11s"


def test_lane_stats_single_lane():
    ledger = {
        "positions": [
            {"status": "settled", "won": True, "pnl_usd": 2.0, "entry_ts": 1.0,
             "title": "Bitcoin Up or Down - July 7, 3AM ET",
             "research": {"series_label": "btc_1h", "window_seconds": 3600}},
            {"status": "open", "entry_ts": 2.0,
             "title": "Ethereum Up or Down - July 7, 3AM ET",
             "research": {"series_label": "eth_1h", "window_seconds": 3600}},
            {"status": "settled", "won": False, "pnl_usd": -5.0, "entry_ts": 3.0,
             "title": "Bitcoin Up or Down - July 7, 8:30PM-8:45PM ET",
             "research": {"series_label": "btc_15m", "series_slug": "btc-up-or-down-15m",
                          "window_seconds": 900}},
        ],
        "stats": {"trades": 1, "settled": 1, "wins": 1, "win_rate": 1.0},
    }
    ls = lane_stats(ledger)
    assert ls["directional"]["settled"] == 2
    assert set(ls.keys()) == {"btc", "eth", "btc_1h", "btc_15m", "eth_1h", "eth_15m", "directional"}
    assert ls["btc"]["settled"] == 2
    assert ls["btc_1h"]["settled"] == 1
    assert ls["btc_15m"]["settled"] == 1
    assert ls["eth"]["open"] == 1
    assert ls["eth_15m"]["settled"] == 0


def test_eth_symbol_trades_filter_and_limit():
    ledger = {
        "positions": [
            {
                "side": "up",
                "entry_ts": float(i),
                "status": "settled",
                "won": True,
                "pnl_usd": 1.0,
                "title": "Ethereum Up or Down - July 7, 3AM ET",
                "research": {"series_label": "eth_1h", "entry_ttc_s": 1200.0},
            }
            for i in range(55)
        ] + [
            {
                "side": "down",
                "entry_ts": 100.0,
                "status": "settled",
                "won": False,
                "pnl_usd": -1.0,
                "title": "Bitcoin Up or Down - July 7, 3AM ET",
                "research": {"series_label": "btc_1h"},
            }
        ],
    }
    eth_rows = symbol_trades_for_dashboard(ledger, symbol="ETH", limit=50)
    assert len(eth_rows) == 50
    assert all(r["trade_symbol"] == "ETH" for r in eth_rows)
    assert eth_rows[0]["sort_ts"] == 54.0
    assert eth_rows[0]["ttm_label"] == "20m"

    eth_stats = symbol_stats(ledger, "ETH")
    assert eth_stats["settled"] == 55
    assert eth_stats["wins"] == 55

    lt = lane_trades_for_dashboard(ledger, limit=50)
    assert len(lt["eth"]) == 50
    assert len(lt["btc"]) == 1
    assert len(lt["directional"]) == 50


def test_lane_trades_per_lane_limit():
    ledger = {
        "positions": [{"side": "up", "entry_ts": float(i), "status": "settled", "won": True,
                       "pnl_usd": 1.0, "research": {"series_label": "btc_1h"}} for i in range(60)],
    }
    lt = lane_trades_for_dashboard(ledger, limit=50)
    assert len(lt["directional"]) == 50
    assert directional_trades_for_dashboard(ledger, limit=50)[0]["sort_ts"] == 59.0


def test_recent_trades_returns_directional_rows():
    ledger = {
        "positions": [
            {
                "side": "down",
                "entry_ts": 1500.0,
                "entry_price": 0.52,
                "status": "settled",
                "won": True,
                "pnl_usd": 2.5,
                "research": {"series_label": "btc_1h"},
            }
        ],
    }
    rows = recent_trades_for_dashboard(ledger, limit=5)
    assert len(rows) == 1
    assert rows[0]["trade_type"] == "directional"
