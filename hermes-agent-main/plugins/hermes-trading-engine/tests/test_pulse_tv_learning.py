"""Fast-learning TradingView signal layer: normalized fields, bucketed performance learning,
best/worst RSI-divergence levels, and promotion diagnostics — all observe-only.

Proves (acceptance #8): bucket stats reconcile with the ledger; promotion requires sample size +
positive EV + clean reconciliation + win-rate; duplicate alerts don't create duplicate candidates;
malformed/stale/wrong-secret rejected; and TradingView cannot bypass the execution gate.
"""

from __future__ import annotations

import json

from engine.pulse.tradingview import (TradingViewIntake, TradingViewSignalLearner, strength_bucket,
                                       BAD_SECRET, STALE_TIMESTAMP, MALFORMED_DIRECTION,
                                       DUPLICATE_EVENT_ID)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig

SECRET = "s3cr3t-token"


# ------------------------------- normalized fields (req #1) -------------------------------- #
def test_signal_level_and_price_parsed():
    intake = TradingViewIntake(secret=SECRET, allowed_symbols=["BTCUSD"], bot_name="hermes")
    raw = json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD",
                      "direction": "UP", "strength": 0.9, "signal_level": "regular",
                      "price": 64010.5, "indicator_name": "RSI Divergence",
                      "event_id": "lvl-1"}).encode()
    code, body = intake.ingest(raw, now=1_000_000.0)
    assert code == 200 and body["accepted"] is True
    ev = intake.latest
    assert ev.signal_level == "regular" and ev.price == 64010.5
    feat = ev.as_feature(now=1_000_000.0)
    assert feat["signal_level"] == "regular" and feat["price"] == 64010.5
    assert feat["strength_bucket"] == ">=0.8"


def test_strength_bucketing():
    assert strength_bucket(0.3) == "<0.5" and strength_bucket(0.6) == "0.5-0.8"
    assert strength_bucket(0.9) == ">=0.8" and strength_bucket(None) == "na"


# ------------------------------- learner buckets + reconcile (req #4,#8) ------------------- #
def _tags(direction="UP", level="regular", strength_b=">=0.8", regime="trending",
          z="-1..1", ttc="120-240s", spread="<=0.01", depth=">=1000"):
    return {"direction": direction, "signal_level": level, "strength_bucket": strength_b,
            "indicator_name": "RSI Divergence", "hurst_regime": regime, "zscore_bucket": z,
            "ttc_bucket": ttc, "spread_bucket": spread, "depth_bucket": depth}


def test_learner_buckets_reconcile_with_settled_count():
    L = TradingViewSignalLearner()
    # 10 settled trades with a signal: 7 wins
    for i in range(10):
        won = i < 7
        L.record_settled(_tags(), won=won, pnl=(2.0 if won else -5.0),
                         ev_after_cost=0.03, reconciled=True)
    rep = L.report()
    assert rep["settled_with_signal"] == 10
    # every dimension's bucket counts sum to the settled count (reconciles with the ledger)
    for dim in TradingViewSignalLearner.DIMS:
        total = sum(b["n"] for b in rep["by_" + dim].values())
        assert total == 10, dim
    assert rep["by_direction"]["UP"]["n"] == 10
    assert rep["by_direction"]["UP"]["win_rate"] == 0.7
    assert rep["by_signal_level"]["regular"]["win_rate"] == 0.7


def test_accepted_rejected_counts():
    L = TradingViewSignalLearner()
    L.record_candidate("UP", accepted=True)
    L.record_candidate("UP", accepted=False)
    L.record_candidate("DOWN", accepted=False)
    rep = L.report()
    assert rep["accepted"] == 1 and rep["rejected"] == 2
    assert rep["accepted_by_direction"]["UP"] == 1
    assert rep["rejected_by_direction"]["DOWN"] == 1


def test_best_and_worst_signal_levels():
    L = TradingViewSignalLearner()
    for _ in range(5):                       # "regular" level: strong
        L.record_settled(_tags(level="regular"), won=True, pnl=2.0, ev_after_cost=0.05,
                         reconciled=True)
    for _ in range(5):                       # "hidden" level: weak
        L.record_settled(_tags(level="hidden"), won=False, pnl=-5.0, ev_after_cost=-0.02,
                         reconciled=True)
    rep = L.report()
    assert rep["best_signal_levels"][0]["signal_level"] == "regular"
    assert rep["worst_signal_levels"][0]["signal_level"] == "hidden"


# ------------------------------- promotion diagnostics (req #6) ---------------------------- #
def test_promotion_requires_all_gates():
    # strong, calibrated, reconciled, enough samples -> eligible
    good = TradingViewSignalLearner()
    for _ in range(60):
        good.record_settled(_tags(level="A+"), won=True, pnl=2.0, ev_after_cost=0.05,
                            reconciled=True)
    rep = good.report(promotion_allowed=False, min_samples=50, min_win_rate=0.8)
    assert rep["promotion"]["any_eligible"] is True
    assert rep["promotion"]["promotion_allowed_by_config"] is False     # diagnostic only
    assert any(b["bucket"] == "A+" for b in rep["promotion"]["eligible_buckets"])

    # below win-rate threshold -> NOT eligible
    weak = TradingViewSignalLearner()
    for i in range(60):
        won = i < 36                          # 60% win rate < 80%
        weak.record_settled(_tags(level="B"), won=won, pnl=(2.0 if won else -5.0),
                           ev_after_cost=0.05, reconciled=True)
    assert weak.report(min_samples=50)["promotion"]["any_eligible"] is False

    # high win-rate but NEGATIVE EV after slippage -> NOT eligible
    negev = TradingViewSignalLearner()
    for _ in range(60):
        negev.record_settled(_tags(level="C"), won=True, pnl=2.0, ev_after_cost=-0.01,
                            reconciled=True)
    assert negev.report(min_samples=50)["promotion"]["any_eligible"] is False

    # high win-rate, positive EV, but UNRECONCILED -> NOT eligible
    unrec = TradingViewSignalLearner()
    for _ in range(60):
        unrec.record_settled(_tags(level="D"), won=True, pnl=2.0, ev_after_cost=0.05,
                            reconciled=False)
    assert unrec.report(min_samples=50)["promotion"]["any_eligible"] is False

    # not enough samples -> NOT eligible
    few = TradingViewSignalLearner()
    for _ in range(10):
        few.record_settled(_tags(level="E"), won=True, pnl=2.0, ev_after_cost=0.05,
                          reconciled=True)
    assert few.report(min_samples=50)["promotion"]["any_eligible"] is False


def test_learner_persists_round_trip():
    L = TradingViewSignalLearner()
    for _ in range(5):
        L.record_settled(_tags(), won=True, pnl=2.0, ev_after_cost=0.04, reconciled=True)
    L.record_candidate("UP", accepted=True)
    L2 = TradingViewSignalLearner()
    L2.load_state(L.to_state())
    assert L2.settled == 5 and L2.accepted == 1
    assert L2.report()["by_direction"]["UP"]["n"] == 5


# ============================ engine-level (reconcile + safety) ============================ #
class _Mkt:
    def __init__(self, w, *, deep=True):
        self._w = w
        self._deep = deep

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
    t0 = 9_870_000.0
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


def _alert(direction="UP"):
    return json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTC/USD",
                       "direction": direction, "strength": 0.9, "signal_level": "regular",
                       "price": 64000.0, "indicator_name": "RSI Divergence",
                       "event_id": f"e-{direction}"}).encode()


def test_engine_bucket_stats_reconcile_with_ledger(tmp_path):
    eng, t0 = _engine(tmp_path, deep=True)
    eng.tradingview.ingest(_alert("UP"), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)                    # settle
    sl = eng.status()["tradingview"]["signal_learning"]
    # number of settled trades that carried a TradingView signal == ledger settled positions w/ TV
    tv_settled = sum(1 for p in eng.ledger.positions.values()
                     if p.status == "settled" and (p.external or {}).get("source") == "tradingview")
    assert sl["settled_with_signal"] == tv_settled >= 1
    for dim in ("direction", "signal_level", "hurst_regime", "ttc_bucket", "spread_bucket"):
        assert sum(b["n"] for b in sl["by_" + dim].values()) == sl["settled_with_signal"]
    assert eng.light_report()["global_reconciled"] is True


def test_engine_tradingview_cannot_bypass_gate_even_with_signal(tmp_path):
    eng, t0 = _engine(tmp_path, deep=False)    # thin book -> gate must reject
    eng.tradingview.ingest(_alert("UP"), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    assert eng.ledger.trades == 0
    eg = eng.ledger.exec_gate_stats()
    assert eg["rejected"]["partial_fill_risk"] >= 1
    assert eng.status()["live_trading_enabled"] is False
    # the signal was still recorded (accepted/rejected counted) but produced no trade
    assert eng.status()["tradingview"]["signal_learning"]["settled_with_signal"] == 0


def test_duplicate_alert_no_duplicate_candidate_then_reject_reasons(tmp_path):
    intake = TradingViewIntake(secret=SECRET, allowed_symbols=["BTCUSD"], bot_name="hermes",
                               data_dir=str(tmp_path))
    a = json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD",
                    "direction": "UP", "event_id": "dup"}).encode()
    intake.ingest(a, now=1_000_000.0)
    intake.ingest(a, now=1_000_001.0)          # duplicate
    assert intake.valid == 1 and intake.reject_reasons[DUPLICATE_EVENT_ID] == 1
    assert len(intake.drain_pending()) == 1    # only ONE candidate signal produced
    # malformed / stale / wrong-secret all rejected
    assert intake.ingest(b"not json", now=1e6)[1]["reason"]  # invalid_json
    assert intake.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD",
                                     "direction": "sideways", "event_id": "m"}).encode(),
                         now=1e6)[1]["reason"] == MALFORMED_DIRECTION
    assert intake.ingest(json.dumps({"secret": "x", "bot_name": "hermes", "symbol": "BTCUSD",
                                     "direction": "UP", "event_id": "w"}).encode(),
                         now=1e6)[1]["reason"] == BAD_SECRET
    assert intake.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTCUSD",
                                     "direction": "UP", "event_id": "s",
                                     "bar_time": 1e6 - 9999}).encode(),
                         now=1e6)[1]["reason"] == STALE_TIMESTAMP
