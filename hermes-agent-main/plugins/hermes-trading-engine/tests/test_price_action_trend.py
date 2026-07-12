"""Tests for spot price-action trend (rising/falling/flat)."""

from __future__ import annotations

from types import SimpleNamespace

from engine.pulse.loop_architecture.asset_triage import (
    PROCEED_SWEEP,
    AssetTriageSkill,
    TriageConfig,
    TriageReject,
)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import OpenSnapshot, PulsePriceFeed
from engine.pulse.price_action_trend import (
    TREND_FALLING,
    TREND_FLAT,
    TREND_RISING,
    compute_trend_from_prices,
    to_triage_feature,
    trend_aligns_side,
    trend_for_window,
)


class _StaticFeed(PulsePriceFeed):
    def __init__(self, price: float):
        super().__init__(fetcher=lambda: price, source_name="test")
        self._last_price = float(price)
        self._last_ts = 1_000_000.0
        self.last_fetch_ok = True

    def poll(self, now=None):
        return self._last_price

    def age_s(self, now=None):
        return 1.0


def test_compute_trend_rising():
    t = compute_trend_from_prices(asset="btc", spot_now=101.0, open_price=100.0, price_age_s=1.0)
    assert t is not None
    assert t["trend"] == TREND_RISING
    assert t["move_from_open_bps"] > 0


def test_compute_trend_falling():
    t = compute_trend_from_prices(asset="eth", spot_now=99.0, open_price=100.0, price_age_s=1.0)
    assert t["trend"] == TREND_FALLING


def test_compute_trend_flat_inside_band():
    t = compute_trend_from_prices(
        asset="btc", spot_now=100.01, open_price=100.0, price_age_s=1.0, min_move_bps=5.0)
    assert t["trend"] == TREND_FLAT


def test_trend_aligns_side():
    assert trend_aligns_side(TREND_RISING, "up")
    assert trend_aligns_side(TREND_FALLING, "down")
    assert not trend_aligns_side(TREND_RISING, "down")
    assert not trend_aligns_side(TREND_FLAT, "up")


def test_trend_for_window_with_open_snapshot():
    feed = _StaticFeed(100.0)
    w = SimpleNamespace(event_id="e1", open_ts=999_990.0, series_slug="btc-up-or-down-hourly")
    feed._opens["e1"] = OpenSnapshot(open_ts=999_990.0, price=100.0, snap_ts=999_991.0)
    feed._last_price = 100.5
    t = trend_for_window(window=w, price_feed=feed, now=1_000_000.0, max_price_age_s=60.0)
    assert t is not None
    assert t["trend"] == TREND_RISING


def _window(ask: float = 0.50) -> PulseWindow:
    book = OrderBook(
        best_bid=ask - 0.02,
        best_ask=ask,
        ask_depth_usd=10000.0,
        bid_depth_usd=10000.0,
        asks=[(ask, 10000.0 / ask)],
        bids=[(ask - 0.02, 10000.0)],
    )
    return PulseWindow(
        event_id="evt-1",
        market_id="m1",
        slug="btc-up-or-down-hourly-test",
        title="BTC hourly",
        open_ts=1_000_000.0,
        close_ts=1_003_600.0,
        up_token_id="up-tok",
        down_token_id="dn-tok",
        series_slug="btc-up-or-down-hourly",
        up_book=book,
        down_book=book,
    )


def test_triage_proceed_with_rising_price_trend():
    skill = AssetTriageSkill(cfg=TriageConfig(trend_source="price"))
    w = _window(0.50)
    feat = to_triage_feature({
        "trend": TREND_RISING,
        "strength": 0.6,
        "timeframe": "spot",
        "asset": "btc",
        "move_from_open_bps": 25.0,
    })
    v = skill.evaluate(window=w, side="up", ask_price=0.50, now=1_000_100.0,
                       tv_feature=feat, symbol="BTCUSD")
    assert v.status == PROCEED_SWEEP


def test_triage_reject_trend_misaligned():
    skill = AssetTriageSkill(cfg=TriageConfig(trend_source="price"))
    w = _window(0.50)
    feat = to_triage_feature({"trend": TREND_FALLING, "strength": 0.6, "timeframe": "spot"})
    v = skill.evaluate(window=w, side="up", ask_price=0.50, now=1_000_100.0,
                       tv_feature=feat, symbol="BTCUSD")
    assert v.status == TriageReject.TREND_MISALIGNED.value
