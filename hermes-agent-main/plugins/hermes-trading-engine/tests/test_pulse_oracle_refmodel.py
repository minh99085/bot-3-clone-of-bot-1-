"""BTC pulse oracle reference model — Chainlink Data Streams ref price via Polymarket RTDS.

Enforces the correct feed architecture:
  * canonical oracle = RTDS crypto_prices_chainlink btc/usd (open/close snapshots);
  * Binance/Coinbase are LEAD features only (never settlement truth);
  * settlement priority = official Polymarket resolution, then RTDS Chainlink proxy only when
    the close-snapshot lag is within threshold;
  * classic Chainlink Data Feed / AggregatorV3 is rejected as a primary settlement feed.
"""

from __future__ import annotations

import pytest

from engine.pulse.oracle import (validate_oracle_feed_type, LeadFeeds, CANONICAL_FEED_TYPE,
                                  REJECTED_FEED_TYPES)
from engine.pulse.settlement import resolve_window, proxy_outcome
from engine.pulse.price import PulsePriceFeed
from engine.pulse.engine import PulseEngine, PulseConfig
from engine.pulse.markets import PulseWindow, OrderBook


class _FakeMarket:
    def __init__(self, resolution=None):
        self.resolution = resolution
    def active_windows(self, now=None, **kw):
        return []
    def hydrate_books(self, w):
        return w
    def fetch_resolution(self, market_id):
        return self.resolution


def _oracle_feed():
    return PulsePriceFeed(fetcher=lambda: 64000.0, source_name="rtds_chainlink",
                          max_open_lag_s=30.0)


# 1) reject classic Chainlink Data Feed / AggregatorV3 as primary settlement -------------- #
def test_reject_classic_chainlink_feed_as_primary_settlement(tmp_path):
    assert validate_oracle_feed_type("chainlink_data_streams_refprice") == CANONICAL_FEED_TYPE
    for bad in ("aggregator_v3", "aggregatorv3", "chainlink_data_feed", "data_feed",
                "latestRoundData"):
        assert bad.lower() in REJECTED_FEED_TYPES
        with pytest.raises(ValueError):
            validate_oracle_feed_type(bad)
    # the engine itself refuses to construct with a classic feed type
    with pytest.raises(ValueError):
        PulseEngine(PulseConfig(oracle_feed_type="aggregator_v3", directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False, data_dir=str(tmp_path)),
                    market_feed=_FakeMarket(), price_feed=_oracle_feed())


# 2) RTDS Chainlink btc/usd used for open/close snapshots --------------------------------- #
def test_open_close_snapshot_source_is_rtds_chainlink(tmp_path):
    feed = _oracle_feed()
    feed.poll(now=1000.0)
    snap = feed.snapshot_open("w", open_ts=1000.0, now=1002.0)
    assert snap.source == "rtds_chainlink"
    eng = PulseEngine(PulseConfig(directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False, data_dir=str(tmp_path)), market_feed=_FakeMarket(),
                      price_feed=feed)
    o = eng.status()["oracle"]
    assert o["oracle_feed_type"] == "chainlink_data_streams_refprice"
    assert o["oracle_symbol"] == "btc/usd"
    assert o["open_snapshot_source"] == "rtds_chainlink"
    assert o["close_snapshot_source"] == "rtds_chainlink"


# 3) Binance/Coinbase are LEAD features only --------------------------------------------- #
def test_lead_feeds_are_features_only(tmp_path):
    assert LeadFeeds.LEAD_ONLY is True
    lf = LeadFeeds(["binance_btcusdt", "coinbase_btcusd"], rtds=None,
                   coinbase_fetcher=lambda: 64010.0)
    lf.poll(now=1000.0)
    feats = lf.features(now=1000.0)
    assert feats["lead_only"] is True
    assert feats["feeds"]["coinbase_btcusd"]["price"] == 64010.0
    # every lead feed is explicitly NOT settlement-eligible
    assert all(f["settlement_eligible"] is False for f in feats["feeds"].values())
    eng = PulseEngine(PulseConfig(directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False, data_dir=str(tmp_path)), market_feed=_FakeMarket(),
                      price_feed=_oracle_feed())
    o = eng.status()["oracle"]
    assert "binance_btcusdt" in o["fast_feed_symbols"] and "coinbase_btcusd" in o["fast_feed_symbols"]
    # settlement only ever uses official Polymarket or the RTDS Chainlink proxy — never a lead
    assert set(o["settlement_sources_used"]) == {"polymarket_resolution", "rtds_chainlink_proxy"}


# 4) settlement priority: Polymarket first, RTDS proxy only within close-lag threshold ----- #
def test_settlement_priority_and_proxy_lag_gate():
    class G:
        def __init__(self, r):
            self.r = r
        def fetch_resolution(self, m):
            return self.r
    # official available -> used first, proxy ignored even if it would disagree
    out, src = resolve_window("m", gamma_feed=G(True), s_open=100.0, s_close=99.0,
                              close_lag_s=5.0, proxy_max_close_lag_s=30.0)
    assert out is True and src == "polymarket_resolution"
    # official not ready, proxy fresh (lag<=threshold) -> proxy
    out, src = resolve_window("m", gamma_feed=G(None), s_open=100.0, s_close=101.0,
                              close_lag_s=10.0, proxy_max_close_lag_s=30.0)
    assert out is True and src == "rtds_chainlink_proxy"
    # official not ready, proxy close-snapshot too stale -> UNRESOLVED (wait for official)
    out, src = resolve_window("m", gamma_feed=G(None), s_open=100.0, s_close=101.0,
                              close_lag_s=120.0, proxy_max_close_lag_s=30.0)
    assert out is None and src == "unresolved"
    assert proxy_outcome(100.0, 100.0) is True       # tie -> Up
    assert proxy_outcome(100.0, 99.9) is False


def test_rtds_subscription_uses_compact_filters_and_parses_updates():
    # RTDS silently drops subscriptions whose filter JSON has spaces -> must be compact.
    from engine.pulse.rtds import _sub_msg, RTDSClient, TOPIC_CHAINLINK
    sub = _sub_msg([(TOPIC_CHAINLINK, "btc/usd")])
    assert '{\\"symbol\\":\\"btc/usd\\"}' in sub        # compact, no space after colon
    assert '": "' not in sub.split('"filters":')[1][:40]
    # parser extracts price from a real-shape chainlink update + skips dumps/heartbeats
    upd = ('{"topic":"crypto_prices_chainlink","type":"update","payload":'
           '{"symbol":"btc/usd","value":64153.46,"timestamp":1782008984000}}')
    assert RTDSClient._parse_update(upd) == ("crypto_prices_chainlink", "btc/usd", 64153.46,
                                             1782008984000)
    assert RTDSClient._parse_update("") is None                       # heartbeat
    assert RTDSClient._parse_update('{"payload":{"data":[]}}') is None  # initial dump (no topic)


def test_full_cycle_settles_via_polymarket_and_reconciles(tmp_path):
    # an end-to-end window settled via official Polymarket, with proxy reconciliation recorded
    t0 = 5_000_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")

    class _M:
        def active_windows(self, now=None, **kw):
            return [win]
        def hydrate_books(self, w):
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=550,
                                  bid_depth_usd=500, asks=[(0.55, 1000.0)], bids=[(0.50, 1000.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=490,
                                    bid_depth_usd=440, asks=[(0.49, 1000.0)], bids=[(0.44, 1000.0)])
            return w
        def fetch_resolution(self, market_id):
            return True
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    from engine.pulse.fair_value import RollingVol
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    eng = PulseEngine(PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02,
                                  basis_buffer=0.0, min_seconds_since_open=0.0,
                                  sigma_trust_floor=0.0, min_vol_samples=2, settle_grace_s=0.0,
                                  proxy_max_close_lag_s=30.0, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False, data_dir=str(tmp_path)),
                      market_feed=_M(), price_feed=feed)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(8):
        eng.tick(now=t0 + 2 + k * 5)
    assert eng.ledger.trades == 1
    eng.tick(now=t0 + 305)
    assert eng.ledger.settled == 1
    assert eng.ledger.settle_sources["polymarket_resolution"] == 1
    # proxy verdict (rising price -> up) reconciled against official (up) -> agree
    assert eng.ledger.recon["both"] == 1 and eng.ledger.recon["agree"] == 1
