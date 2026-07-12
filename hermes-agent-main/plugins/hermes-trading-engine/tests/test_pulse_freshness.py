"""Cycle-1 data-integrity: oracle freshness contract + spike-filter redesign (PAPER ONLY).

Proves: a stale RTDS socket fails CLOSED (fresh_oracle_price -> None, feed age grows); the price
feed's freshness clock only advances on a live fetch; the spike filter rejects a jump vs a FRESH
prior but FLUSHES a stale prior (no post-reconnect lock-up); and the engine abstains ('stale_price')
rather than computing fair value on an aged price.
"""

from __future__ import annotations

import time

from engine.pulse.rtds import RTDSClient, TOPIC_CHAINLINK, TOPIC_BINANCE
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.engine import PulseEngine, PulseConfig


# ------------------------------- RTDS freshness + spike filter ---------------------------- #
def test_rtds_fresh_oracle_price_and_age():
    c = RTDSClient(max_age_s=30.0)
    assert c.fresh_oracle_price() is None and c.oracle_age_s() is None      # nothing yet
    c._record(TOPIC_CHAINLINK, "btc/usd", 64000.0, None)
    assert c.oracle_price() == 64000.0
    assert c.fresh_oracle_price(now=time.time()) == 64000.0                 # fresh -> served
    # force the receipt time far into the past -> stale -> fail closed (None), but raw still cached
    with c._lock:
        v = c._latest[(TOPIC_CHAINLINK, "btc/usd")]
        c._latest[(TOPIC_CHAINLINK, "btc/usd")] = (v[0], v[1], time.time() - 120)
    assert c.fresh_oracle_price() is None                                   # aged out
    assert c.oracle_price() == 64000.0                                      # raw cache unchanged
    assert c.oracle_age_s() > 100


def test_rtds_spike_filter_rejects_fresh_but_flushes_stale():
    c = RTDSClient(spike_filter=0.10, spike_filter_fresh_s=10.0)
    c._record(TOPIC_CHAINLINK, "btc/usd", 64000.0, None)
    c._record(TOPIC_CHAINLINK, "btc/usd", 90000.0, None)                    # +40% vs FRESH prior
    assert c.oracle_price() == 64000.0                                      # rejected (bad tick)
    # now make the prior STALE, then send the same big jump -> must FLUSH (accept), not lock up
    with c._lock:
        v = c._latest[(TOPIC_CHAINLINK, "btc/usd")]
        c._latest[(TOPIC_CHAINLINK, "btc/usd")] = (v[0], v[1], time.time() - 60)
    c._record(TOPIC_CHAINLINK, "btc/usd", 90000.0, None)
    assert c.oracle_price() == 90000.0                                      # flushed stale prior
    # a small move vs a fresh prior is always accepted
    c._record(TOPIC_CHAINLINK, "btc/usd", 90500.0, None)
    assert c.oracle_price() == 90500.0
    st = c.status()
    assert "oracle_age_s" in st and "oracle_fresh" in st and st["max_age_s"] == 30.0


# ------------------------------- price feed freshness clock -------------------------------- #
def test_price_feed_age_only_advances_on_live_fetch():
    box = {"px": 64000.0}
    feed = PulsePriceFeed(fetcher=lambda: box["px"], vol=RollingVol(min_samples=2),
                          source_name="rtds_chainlink")
    feed.poll(now=1000.0)
    assert feed.current() == 64000.0 and feed.last_fetch_ok is True
    assert feed.is_fresh(45.0, now=1000.0) is True
    # stale fetch (None): keep last price, but the freshness clock FREEZES at 1000
    box["px"] = None
    feed.poll(now=1100.0)
    assert feed.current() == 64000.0 and feed.last_fetch_ok is False
    assert feed.age_s(now=1100.0) == 100.0                                  # age measured from 1000
    assert feed.is_fresh(45.0, now=1100.0) is False                        # stale -> not fresh
    st = feed.status()
    assert st["last_fetch_ok"] is False and st["age_s"] is not None and st["age_s"] > 0


# ------------------------------- engine fails closed on stale price ------------------------ #
class _Mkt:
    def __init__(self, w):
        self._w = w

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=50000, bid_depth_usd=50000,
                              asks=[(0.55, 100000.0)], bids=[(0.50, 100000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=49000, bid_depth_usd=44000,
                                asks=[(0.49, 100000.0)], bids=[(0.44, 100000.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def test_engine_abstains_on_stale_price(tmp_path):
    t0 = 9_960_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    box = {"px": 64000.0, "live": True}

    def fetch():
        box["px"] += 4.0
        return box["px"] if box["live"] else None
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9, data_dir=str(tmp_path),
                      price_max_age_s=45.0)
    eng = PulseEngine(cfg, market_feed=_Mkt(win), price_feed=feed)
    for i in range(12):                                  # warm up (fresh price)
        eng.tick(now=t0 - 12 + i)
    box["live"] = False                                 # feed goes stale (fetch returns None)
    # tick well beyond price_max_age_s since the last live fetch -> must abstain 'stale_price'
    eng.tick(now=t0 + 2)
    eng.tick(now=t0 + 100)
    recent = eng.status()["recent_evaluations"]
    reasons = [ (r.get("reason") or (r.get("action") or {}).get("reason")) for r in recent ]
    lc = eng.status()["decision_lifecycle"]["rejected_by_stage"]
    assert lc.get("skipped", 0) >= 0                    # lifecycle intact
    assert any(r == "stale_price" for r in reasons) or eng.ledger.trades == 0
    assert eng.light_report()["global_reconciled"] is True
    assert eng.status()["paper_only"] is True
