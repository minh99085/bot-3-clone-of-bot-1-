"""Dual-market: Polymarket 5m + 15m series ingestion and per-series reporting."""

from __future__ import annotations

from engine.pulse.markets import (
    PulseMarketFeed, MultiSeriesMarketFeed, SERIES_SLUG_5M, SERIES_SLUG_15M,
    WINDOW_SECONDS, WINDOW_SECONDS_15M,
)
from engine.pulse.reporting import ledger_stats_by_market_series
from engine.pulse.executor import PulsePosition


def test_parse_15m_window_from_slug():
    ev = {
        "id": "ev15", "slug": "btc-updown-15m-9960000",
        "title": "BTC 15m", "markets": [{
            "id": "m15", "clobTokenIds": '["u","d"]', "outcomes": '["Up","Down"]',
            "endDate": "2026-01-01T00:15:00Z",
        }],
    }
    w = PulseMarketFeed.parse_window(ev, series_slug=SERIES_SLUG_15M)
    assert w is not None
    assert w.window_seconds == WINDOW_SECONDS_15M
    assert w.close_ts - w.open_ts == WINDOW_SECONDS_15M
    assert w.series_label == "15m"


def test_parse_5m_window_unchanged():
    ev = {
        "id": "ev5", "slug": "btc-updown-5m-9960000",
        "title": "BTC 5m", "markets": [{
            "id": "m5", "clobTokenIds": '["u","d"]', "outcomes": '["Up","Down"]',
            "endDate": "2026-01-01T00:05:00Z",
        }],
    }
    w = PulseMarketFeed.parse_window(ev, series_slug=SERIES_SLUG_5M)
    assert w.window_seconds == WINDOW_SECONDS
    assert w.series_label == "5m"


def test_multi_series_feed_merges_windows():
    t0 = 1_780_000_000.0

    def fake_http(url, params):
        slug = params.get("series_slug")
        if slug == SERIES_SLUG_5M:
            open_ts = t0
            dur = WINDOW_SECONDS
            ev_slug = "btc-updown-5m-%d" % int(open_ts)
        else:
            open_ts = t0
            dur = WINDOW_SECONDS_15M
            ev_slug = "btc-updown-15m-%d" % int(open_ts)
        return 200, [{
            "id": "e-%s" % slug, "slug": ev_slug, "title": slug,
            "markets": [{"id": "m-%s" % slug, "clobTokenIds": '["u","d"]',
                         "outcomes": '["Up","Down"]'}],
        }]

    feed = MultiSeriesMarketFeed((SERIES_SLUG_5M, SERIES_SLUG_15M), http_get=fake_http)
    wins = feed.active_windows(now=t0 + 10)
    labels = {w.series_label for w in wins}
    assert labels == {"5m", "15m"}
    rep = feed.report()
    assert rep["multi_series"] is True
    assert SERIES_SLUG_15M in rep["feeds"]


def test_ledger_stats_by_market_series():
    positions = {
        "a": PulsePosition(
            window_key="a", market_id="m1", title="5m", side="down",
            token_id="t", entry_price=0.55, size_usd=5.0, shares=9.0,
            fair_at_entry=0.5, edge_at_entry=0.02, open_ts=1.0, close_ts=301.0,
            entry_ts=2.0, status="settled", won=True, pnl_usd=4.0,
            research={"market_series": "5m", "series_label": "5m"}),
        "b": PulsePosition(
            window_key="b", market_id="m2", title="15m", side="up",
            token_id="t2", entry_price=0.6, size_usd=5.0, shares=8.33,
            fair_at_entry=0.55, edge_at_entry=0.03, open_ts=1.0, close_ts=901.0,
            entry_ts=2.0, status="settled", won=False, pnl_usd=-5.0,
            research={"market_series": "15m", "series_label": "15m"}),
    }
    stats = ledger_stats_by_market_series(positions)
    assert len(stats) == 2
    five = next(v for v in stats.values() if v["series_label"] == "5m")
    fifteen = next(v for v in stats.values() if v["series_label"] == "15m")
    assert five["settled"] == 1 and five["wins"] == 1
    assert fifteen["settled"] == 1 and fifteen["wins"] == 0