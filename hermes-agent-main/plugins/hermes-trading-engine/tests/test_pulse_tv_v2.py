"""TradingView Composite v2 schema: VWAP / Bollinger / volume / HTF-bias features.

Proves: optional field parsing, invalid-enum coercion to "unknown" (alert still accepted),
missing-field safety, v2 report buckets reconcile with the ledger, the v2 report fields exist,
and TradingView (with v2 fields) cannot bypass the execution gate.
"""

from __future__ import annotations

import json

from engine.pulse.tradingview import (TradingViewIntake, TradingViewSignalLearner,
                                       VWAP_STATES, BB_STATES, VOLUME_STATES, HTF_BIASES)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig

SECRET = "s3cr3t-token"


def _intake():
    return TradingViewIntake(secret=SECRET, allowed_symbols=["BTCUSD"], bot_name="hermes")


def _alert(**over):
    base = {"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD", "direction": "UP",
            "event_id": "v2-1"}
    base.update(over)
    return json.dumps(base).encode()


# ------------------------------- optional field parsing ------------------------------------ #
def test_v2_fields_parsed():
    intake = _intake()
    code, body = intake.ingest(_alert(
        vwap_state="reclaim", bb_state="squeeze", relative_volume=2.4, volume_state="spike",
        htf_bias="bullish", composite_version="v2", signal_level="regular"), now=1_000_000.0)
    assert code == 200 and body["accepted"] is True
    ev = intake.latest
    assert ev.vwap_state == "reclaim" and ev.bb_state == "squeeze"
    assert ev.relative_volume == 2.4 and ev.volume_state == "spike"
    assert ev.htf_bias == "bullish" and ev.composite_version == "v2"
    feat = ev.as_feature(now=1_000_000.0)
    for k in ("vwap_state", "bb_state", "relative_volume", "volume_state", "htf_bias",
              "composite_version"):
        assert k in feat
    # all enum values are valid members of their sets
    assert ev.vwap_state in VWAP_STATES and ev.bb_state in BB_STATES
    assert ev.volume_state in VOLUME_STATES and ev.htf_bias in HTF_BIASES


def test_v2_invalid_enum_coerced_to_unknown():
    intake = _intake()
    code, body = intake.ingest(_alert(
        vwap_state="diagonal", bb_state="??", volume_state="loud", htf_bias="sideways",
        event_id="v2-bad"), now=1_000_000.0)
    assert code == 200 and body["accepted"] is True   # invalid enum does NOT reject the alert
    ev = intake.latest
    assert ev.vwap_state == "unknown" and ev.bb_state == "unknown"
    assert ev.volume_state == "unknown" and ev.htf_bias == "unknown"


def test_v2_missing_fields_safe():
    intake = _intake()
    code, body = intake.ingest(_alert(event_id="v2-missing"), now=1_000_000.0)   # no v2 fields
    assert code == 200 and body["accepted"] is True
    ev = intake.latest
    assert ev.vwap_state == "unknown" and ev.bb_state == "unknown"
    assert ev.volume_state == "unknown" and ev.htf_bias == "unknown"
    assert ev.relative_volume is None and ev.composite_version is None


# ------------------------------- learner buckets + report fields --------------------------- #
def test_v2_learner_buckets_reconcile_and_report_fields():
    L = TradingViewSignalLearner()
    tags = {"direction": "UP", "signal_level": "regular", "vwap_state": "above",
            "bb_state": "expansion_up", "volume_state": "spike", "htf_bias": "bullish",
            "composite_version": "v2"}
    for i in range(12):
        L.record_settled(tags, won=(i % 2 == 0), pnl=(2.0 if i % 2 == 0 else -5.0),
                         ev_after_cost=0.03, reconciled=True)
    rep = L.report()
    for fld in ("by_vwap_state", "by_bb_state", "by_volume_state", "by_htf_bias",
                "by_composite_version", "by_signal_level", "best_buckets", "worst_buckets",
                "promotion"):
        assert fld in rep, fld
    # each v2 dimension's bucket counts reconcile with the settled count
    for dim in ("vwap_state", "bb_state", "volume_state", "htf_bias", "composite_version"):
        assert sum(b["n"] for b in rep["by_" + dim].values()) == 12, dim
    assert rep["by_vwap_state"]["above"]["n"] == 12
    assert rep["by_composite_version"]["v2"]["win_rate"] == 0.5


# ------------------------------- engine: reconcile + cannot bypass gate -------------------- #
class _Mkt:
    def __init__(self, w, *, deep):
        self._w, self._deep = w, deep

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        if self._deep:
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=50000,
                                  bid_depth_usd=50000, asks=[(0.55, 100000.0)],
                                  bids=[(0.50, 100000.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=49000,
                                    bid_depth_usd=44000, asks=[(0.49, 100000.0)],
                                    bids=[(0.44, 100000.0)])
        else:
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=2.0,
                                  bid_depth_usd=2.0, asks=[(0.55, 1.0)], bids=[(0.50, 1.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=2.0,
                                    bid_depth_usd=2.0, asks=[(0.49, 1.0)], bids=[(0.44, 1.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _engine(tmp_path, *, deep):
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
                      tradingview_allowed_symbols=("BTC/USD", "BTCUSD"), data_dir=str(tmp_path))
    return PulseEngine(cfg, market_feed=_Mkt(win, deep=deep), price_feed=feed), t0


def _v2_alert():
    return json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTC/USD",
                       "direction": "UP", "signal_level": "regular", "vwap_state": "above",
                       "bb_state": "expansion_up", "volume_state": "spike", "htf_bias": "bullish",
                       "relative_volume": 2.1, "composite_version": "v2",
                       "event_id": "v2-eng"}).encode()


def test_engine_v2_buckets_reconcile_with_ledger(tmp_path):
    eng, t0 = _engine(tmp_path, deep=True)
    eng.tradingview.ingest(_v2_alert(), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)                          # settle
    sl = eng.status()["tradingview"]["signal_learning"]
    tv_settled = sum(1 for p in eng.ledger.positions.values()
                     if p.status == "settled" and (p.external or {}).get("source") == "tradingview")
    assert sl["settled_with_signal"] == tv_settled >= 1
    for dim in ("vwap_state", "bb_state", "volume_state", "htf_bias", "composite_version"):
        assert sum(b["n"] for b in sl["by_" + dim].values()) == sl["settled_with_signal"]
    assert sl["by_vwap_state"].get("above", {}).get("n") == sl["settled_with_signal"]
    assert eng.light_report()["global_reconciled"] is True


def test_engine_v2_tradingview_cannot_bypass_gate(tmp_path):
    eng, t0 = _engine(tmp_path, deep=False)         # thin book -> gate must reject
    eng.tradingview.ingest(_v2_alert(), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    assert eng.ledger.trades == 0                   # v2 features cannot force a fill past the gate
    assert eng.ledger.exec_gate_stats()["rejected"]["partial_fill_risk"] >= 1
    assert eng.status()["live_trading_enabled"] is False
