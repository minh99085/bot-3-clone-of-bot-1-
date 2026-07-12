"""ETH directional oracle: asset-matched pricing for ETH hourly up/down windows (PAPER ONLY)."""

from __future__ import annotations

from types import SimpleNamespace

from engine.pulse.engine import PulseConfig, PulseEngine
from engine.pulse.rtds import RTDSClient, TOPIC_CHAINLINK
from engine.pulse.price import PulsePriceFeed


def _window(series_slug: str, series_label: str):
    return SimpleNamespace(series_slug=series_slug, series_label=series_label,
                           directional_lane=True, market_kind="updown")


def test_window_asset_classifies_eth_vs_btc():
    assert PulseEngine._window_asset(_window("eth-up-or-down-hourly", "eth_1h")) == "eth"
    assert PulseEngine._window_asset(_window("btc-up-or-down-hourly", "btc_1h")) == "btc"
    assert PulseEngine._window_asset(_window("", "eth_1h")) == "eth"
    assert PulseEngine._window_asset(_window("", "5m")) == "btc"


def test_needs_eth_oracle_flag_and_slugs():
    cfg = PulseConfig(directional_hourly_discover=True)
    assert PulseEngine._needs_eth_oracle(SimpleNamespace(cfg=cfg)) is True

    cfg2 = PulseConfig(directional_hourly_discover=False,
                       directional_series_slugs=("btc-up-or-down-hourly",))
    assert PulseEngine._needs_eth_oracle(SimpleNamespace(cfg=cfg2)) is False

    cfg3 = PulseConfig(directional_hourly_discover=False,
                       directional_series_slugs=("btc-up-or-down-hourly", "eth-up-or-down-hourly"))
    assert PulseEngine._needs_eth_oracle(SimpleNamespace(cfg=cfg3)) is True


def test_price_feed_routing_by_asset():
    btc_feed = PulsePriceFeed(fetcher=lambda: 100.0, source_name="btc")
    eth_feed = PulsePriceFeed(fetcher=lambda: 10.0, source_name="eth")
    stub = SimpleNamespace(price=btc_feed, _eth_price=eth_feed,
                           _window_asset=PulseEngine._window_asset)

    eth_w = _window("eth-up-or-down-hourly", "eth_1h")
    btc_w = _window("btc-up-or-down-hourly", "btc_1h")
    assert PulseEngine._price_feed_for(stub, eth_w) is eth_feed
    assert PulseEngine._price_feed_for(stub, btc_w) is btc_feed

    stub_no_eth = SimpleNamespace(price=btc_feed, _eth_price=None,
                                  _window_asset=PulseEngine._window_asset)
    assert PulseEngine._price_feed_for(stub_no_eth, eth_w) is btc_feed


def test_hourly_routes_to_binance_feeds_not_chainlink():
    btc_chainlink = PulsePriceFeed(fetcher=lambda: 100.0, source_name="chainlink_btc")
    eth_chainlink = PulsePriceFeed(fetcher=lambda: 10.0, source_name="chainlink_eth")
    btc_binance = PulsePriceFeed(fetcher=lambda: 101.0, source_name="binance_btcusdt")
    eth_binance = PulsePriceFeed(fetcher=lambda: 11.0, source_name="binance_ethusdt")
    stub = SimpleNamespace(
        price=btc_chainlink, _eth_price=eth_chainlink,
        _btc_hourly_price=btc_binance, _eth_hourly_price=eth_binance,
        _window_asset=PulseEngine._window_asset)
    btc_1h = SimpleNamespace(series_slug="btc-up-or-down-hourly", series_label="btc_1h",
                             window_seconds=3600)
    eth_1h = SimpleNamespace(series_slug="eth-up-or-down-hourly", series_label="eth_1h",
                             window_seconds=3600)
    btc_15m = SimpleNamespace(series_slug="btc-up-or-down-15m", series_label="btc_15m",
                              window_seconds=900)
    assert PulseEngine._price_feed_for(stub, btc_1h) is btc_binance
    assert PulseEngine._price_feed_for(stub, eth_1h) is eth_binance
    assert PulseEngine._price_feed_for(stub, btc_15m) is btc_chainlink


def test_settle_feed_routing_by_position_asset():
    btc_feed = PulsePriceFeed(fetcher=lambda: 100.0, source_name="btc")
    eth_feed = PulsePriceFeed(fetcher=lambda: 10.0, source_name="eth")
    stub = SimpleNamespace(price=btc_feed, _eth_price=eth_feed)
    eth_pos = SimpleNamespace(research={"asset": "eth"})
    btc_pos = SimpleNamespace(research={"asset": "btc"})
    none_pos = SimpleNamespace(research=None)
    assert PulseEngine._settle_price_feed_for(stub, eth_pos) is eth_feed
    assert PulseEngine._settle_price_feed_for(stub, btc_pos) is btc_feed
    assert PulseEngine._settle_price_feed_for(stub, none_pos) is btc_feed


def test_rtds_fresh_price_fail_closed():
    c = RTDSClient(subscriptions=[(TOPIC_CHAINLINK, "eth/usd")])
    now = 1_000_000.0
    c._latest[(TOPIC_CHAINLINK, "eth/usd")] = (2500.0, None, now)
    assert c.fresh_price(TOPIC_CHAINLINK, "eth/usd", max_age_s=30.0, now=now + 5) == 2500.0
    assert c.fresh_price(TOPIC_CHAINLINK, "eth/usd", max_age_s=30.0, now=now + 60) is None
    assert c.fresh_price(TOPIC_CHAINLINK, "btc/usd", max_age_s=30.0, now=now) is None


def test_directional_windows_passes_eth_oracle_not_btc_fallback(tmp_path):
    """Regression: ETH spot for discovery must come from the ETH oracle, not BTC fallback."""
    eng = PulseEngine(PulseConfig(
        data_dir=str(tmp_path),
        directional_hourly_discover=True,
        directional_series_slugs=("btc-up-or-down-hourly", "eth-up-or-down-hourly"),
    ))
    btc_feed = PulsePriceFeed(fetcher=lambda: 100_000.0, source_name="btc")
    eth_feed = PulsePriceFeed(fetcher=lambda: 3_500.0, source_name="eth")
    btc_feed.poll()
    eth_feed.poll()
    eng.price = btc_feed
    eng._eth_price = eth_feed
    eng.leads = SimpleNamespace(_latest={"binance_ethusdt": (99_999.0, 0.0)})

    captured = {}

    class _Feed:
        def active_windows(self, *, now, btc_spot, eth_spot):
            captured["btc_spot"] = btc_spot
            captured["eth_spot"] = eth_spot
            return []

    eng._directional_hourly_feed = _Feed()
    eng._directional_windows(1_000_000.0)
    assert captured["btc_spot"] == 100_000.0
    assert captured["eth_spot"] == 3_500.0
    assert captured["eth_spot"] != captured["btc_spot"]
