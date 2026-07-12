"""TradingView Composite v4 order-flow / event schema: cvd_state, funding_state, liquidation_spike,
event_blackout (all OBSERVE-ONLY).

Proves: valid v4 parsing, missing-optional safety, invalid-enum coercion to "unknown", learner
buckets, persistence round-trip, and that v4 fields stay observe-only (cannot place/bypass a trade;
event_blackout does NOT trigger a real blackout — it is measured only).
"""

from __future__ import annotations

import json

from engine.pulse.tradingview import (TradingViewIntake, TradingViewSignalLearner,
                                       CVD_STATES, FUNDING_STATES)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig

SECRET = "s3cr3t-token"


def _intake(tmp_path=None):
    return TradingViewIntake(secret=SECRET, allowed_symbols=["BTCUSD"], bot_name="hermes",
                             data_dir=(str(tmp_path) if tmp_path else None))


def _alert(**over):
    base = {"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD", "direction": "UP",
            "event_id": "v4-1"}
    base.update(over)
    return json.dumps(base).encode()


def test_v4_fields_parsed():
    intake = _intake()
    code, body = intake.ingest(_alert(
        cvd_state="bullish", funding_state="negative", liquidation_spike=True,
        event_blackout=False, composite_version="v4"), now=1_000_000.0)
    assert code == 200 and body["accepted"] is True
    ev = intake.latest
    assert ev.cvd_state == "bullish" and ev.funding_state == "negative"
    assert ev.liquidation_spike is True and ev.event_blackout is False
    feat = ev.as_feature(now=1_000_000.0)
    for k in ("cvd_state", "funding_state", "liquidation_spike", "event_blackout"):
        assert k in feat
    assert ev.cvd_state in CVD_STATES and ev.funding_state in FUNDING_STATES
    assert ev.to_dict()["cvd_state"] == "bullish"


def test_v4_accepts_composite_pine_vocabulary():
    # the Composite v4 Pine script emits buy_pressure/sell_pressure + long_crowded/short_crowded;
    # these must be captured (not coerced to "unknown") so the data is usable for learning.
    intake = _intake()
    intake.ingest(_alert(cvd_state="buy_pressure", funding_state="long_crowded",
                         event_id="v4-vocab"), now=1_000_000.0)
    ev = intake.latest
    assert ev.cvd_state == "buy_pressure" and ev.funding_state == "long_crowded"
    assert ev.cvd_state in CVD_STATES and ev.funding_state in FUNDING_STATES
    intake.ingest(_alert(cvd_state="sell_pressure", funding_state="short_crowded",
                         event_id="v4-vocab2"), now=1_000_001.0)
    assert intake.latest.cvd_state == "sell_pressure"


def test_v4_missing_fields_safe():
    intake = _intake()
    code, body = intake.ingest(_alert(event_id="v4-missing"), now=1_000_000.0)
    assert code == 200 and body["accepted"] is True
    ev = intake.latest
    assert ev.cvd_state == "unknown" and ev.funding_state == "unknown"
    assert ev.liquidation_spike is None and ev.event_blackout is None


def test_v4_invalid_enums_coerced():
    intake = _intake()
    code, body = intake.ingest(_alert(
        cvd_state="explode", funding_state="huge", liquidation_spike="maybe",
        event_blackout="sometimes", event_id="v4-bad"), now=1_000_000.0)
    assert code == 200 and body["accepted"] is True       # bad values never reject the alert
    ev = intake.latest
    assert ev.cvd_state == "unknown" and ev.funding_state == "unknown"
    assert ev.liquidation_spike is None and ev.event_blackout is None


def test_v4_learner_buckets():
    L = TradingViewSignalLearner()
    tags = {"direction": "UP", "cvd_state": "bullish", "funding_state": "negative",
            "liquidation_spike": True, "event_blackout": False}
    for i in range(10):
        L.record_settled(tags, won=(i % 2 == 0), pnl=(2.0 if i % 2 == 0 else -5.0),
                         ev_after_cost=0.03, reconciled=True)
    rep = L.report()
    for fld in ("by_cvd_state", "by_funding_state", "by_liquidation_spike", "by_event_blackout"):
        assert fld in rep, fld
    assert rep["by_cvd_state"]["bullish"]["n"] == 10
    assert rep["by_liquidation_spike"]["True"]["n"] == 10
    assert rep["by_funding_state"]["negative"]["win_rate"] == 0.5


def test_v4_persistence_roundtrip(tmp_path):
    intake = _intake(tmp_path)
    intake.ingest(_alert(cvd_state="bearish", funding_state="extreme_positive",
                         liquidation_spike=True, event_blackout=True, event_id="v4-persist"),
                  now=1_000_000.0)
    restored = _intake(tmp_path)                           # reloads persisted latest signal
    ev = restored.latest
    assert ev is not None and ev.cvd_state == "bearish" and ev.funding_state == "extreme_positive"
    assert ev.liquidation_spike is True and ev.event_blackout is True


# ------------------------------- engine: observe-only + reconcile -------------------------- #
class _Mkt:
    def __init__(self, w):
        self._w = w

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=50000,
                              bid_depth_usd=50000, asks=[(0.55, 100000.0)], bids=[(0.50, 100000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=49000,
                                bid_depth_usd=44000, asks=[(0.49, 100000.0)], bids=[(0.44, 100000.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def test_engine_v4_observe_only_and_reconciles(tmp_path):
    t0 = 9_930_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      tradingview_secret=SECRET, tradingview_webhook_port=0,
                      tradingview_allowed_symbols=("BTC/USD",), data_dir=str(tmp_path))
    eng = PulseEngine(cfg, market_feed=_Mkt(win), price_feed=feed)
    eng.tradingview.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTC/USD",
                                       "direction": "UP", "cvd_state": "bullish",
                                       "funding_state": "negative", "liquidation_spike": True,
                                       "event_blackout": True, "event_id": "v4-eng"}).encode(),
                           now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)
    # event_blackout=True from TV must NOT have blocked trading (observe-only): a trade still happened
    assert eng.ledger.trades >= 1
    sl = eng.status()["tradingview"]["signal_learning"]
    for dim in ("cvd_state", "funding_state", "liquidation_spike", "event_blackout"):
        assert sum(b["n"] for b in sl["by_" + dim].values()) == sl["settled_with_signal"] >= 1
    assert eng.light_report()["global_reconciled"] is True
    if eng.webhook is not None:
        eng.webhook.stop()
