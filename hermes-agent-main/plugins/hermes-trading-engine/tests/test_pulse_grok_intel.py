"""Grok signal-intelligence layer (A analyst + B predictor) — OBSERVE-ONLY, off hot path.

All Grok calls are mocked (no network in CI). Proves: shared budget caps cost/rate; B predicts +
is graded leakage-free and is fail-open + budget-gated; A produces a research note + fail-open;
and B cannot bypass the execution gate or trade.
"""

from __future__ import annotations

import json

from engine.pulse.grok_intel import (GrokBudget, GrokSignalPredictor, GrokSignalAnalyst)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig

SECRET = "s3cr3t-token"


# ------------------------------- shared budget guard --------------------------------------- #
def test_budget_daily_and_hourly_caps():
    b = GrokBudget(daily_usd_cap=0.05, est_usd_per_call=0.02,
                   per_feature_hourly={"predictor": 10})
    assert b.try_spend("predictor", now=1000.0) is True     # $0.02
    assert b.try_spend("predictor", now=1000.0) is True     # $0.04
    assert b.try_spend("predictor", now=1000.0) is False    # $0.06 > $0.05 daily cap
    # next day resets
    assert b.try_spend("predictor", now=1000.0 + 90000) is True


def test_budget_per_feature_hourly_cap():
    b = GrokBudget(daily_usd_cap=100.0, est_usd_per_call=0.001,
                   per_feature_hourly={"analyst": 2})
    assert b.try_spend("analyst", now=0.0) is True
    assert b.try_spend("analyst", now=10.0) is True
    assert b.try_spend("analyst", now=20.0) is False        # 3rd within the hour
    assert b.try_spend("analyst", now=3700.0) is True        # an hour later, ok


# ------------------------------- B: per-signal predictor (mocked) -------------------------- #
def test_predictor_predicts_scores_leakage_free():
    p = GrokSignalPredictor(predictor_fn=lambda ctx: 0.8, budget=None)
    p.request("e1", {"signal": {"direction": "UP"}})
    assert p._process_one() is True            # worker step (synchronous for the test)
    assert p.get("e1")["p_up"] == 0.8
    # score against a realized UP move -> correct; brier = (0.8-1)^2 = 0.04
    p.score("e1", True)
    rep = p.report()
    assert rep["observe_only"] is True and rep["affects_trading"] is False
    assert rep["predicted"] == 1 and rep["scored"] == 1 and rep["accuracy"] == 1.0
    assert abs(rep["brier"] - 0.04) < 1e-9


def test_predictor_fail_open_and_budget_skip():
    # fail-open: predictor_fn returns None -> error, no result
    p = GrokSignalPredictor(predictor_fn=lambda ctx: None, budget=None)
    p.request("x", {})
    p._process_one()
    assert p.get("x") is None and p.report()["errors"] == 1
    # budget exhausted -> skipped, no call
    spent = GrokBudget(daily_usd_cap=0.0, est_usd_per_call=0.02)
    p2 = GrokSignalPredictor(predictor_fn=lambda ctx: 0.9, budget=spent)
    p2.request("y", {})
    p2._process_one()
    assert p2.get("y") is None and p2.report()["skipped_budget"] == 1


def test_predictor_dedupes_requests_and_persists():
    p = GrokSignalPredictor(predictor_fn=lambda ctx: 0.6, budget=None)
    p.request("dup", {})
    p.request("dup", {})                       # duplicate event_id -> ignored
    assert p.requested == 1
    p._process_one()
    p.score("dup", False)
    p2 = GrokSignalPredictor(predictor_fn=lambda ctx: 0.6)
    p2.load_state(p.to_state())
    assert p2.scored == 1 and p2.report()["predicted"] == 1


# ------------------------------- A: batch analyst (mocked) --------------------------------- #
def test_analyst_produces_note_and_fail_open():
    note = {"summary": "regular RSI div looks predictive in calm regimes",
            "working": ["signal_level=regular"], "failing": ["zscore_bucket=-1..1"],
            "warnings": ["small sample on hidden"]}
    a = GrokSignalAnalyst(analyst_fn=lambda r: note,
                          report_provider=lambda: {"signal_learning": {"settled_with_signal": 40}})
    a.refresh()
    rep = a.report()
    assert rep["observe_only"] is True and rep["affects_trading"] is False
    assert rep["last_note"]["summary"].startswith("regular RSI")
    assert rep["calls"] == 1
    # fail-open: analyst_fn returns None -> error, note unchanged
    a2 = GrokSignalAnalyst(analyst_fn=lambda r: None, report_provider=lambda: {})
    a2.refresh()
    assert a2.report()["last_note"] is None and a2.report()["errors"] == 1


def test_analyst_learns_with_continuity_and_history():
    """A feeds its PRIOR analysis back in (continuity) and keeps a rolling history, so it refines as
    the bot's evidence grows."""
    seen = []

    def _fn(report):
        seen.append(report)                          # capture what the analyst was given
        i = len(seen)
        return {"summary": "analysis %d" % i, "changes_since_last": ["delta %d" % i],
                "focus_next": ["watch trending"]}
    a = GrokSignalAnalyst(analyst_fn=_fn,
                          report_provider=lambda: {"learned_selectivity": {"rejected": 5}})
    a.refresh()
    a.refresh()
    # 2nd call's report carried the 1st note as prior_analysis + the running count (continuity)
    assert seen[0]["prior_analysis"] is None and seen[0]["analyses_done"] == 0
    assert seen[1]["prior_analysis"]["summary"] == "analysis 1" and seen[1]["analyses_done"] == 1
    rep = a.report()
    assert rep["last_note"]["summary"] == "analysis 2"
    assert rep["last_note"]["changes_since_last"] == ["delta 2"]
    assert len(rep["history"]) == 2 and rep["learns_from"] == "bot_growing_evidence_with_continuity"
    # history survives a persist/restore round-trip
    restored = GrokSignalAnalyst(analyst_fn=lambda r: None)
    restored.load_state(a.to_state())
    assert len(restored.report()["history"]) == 2


def test_analyst_budget_skip_and_persist():
    spent = GrokBudget(daily_usd_cap=0.0, est_usd_per_call=0.02)
    a = GrokSignalAnalyst(analyst_fn=lambda r: {"summary": "x"}, budget=spent,
                          report_provider=lambda: {})
    a.refresh()
    assert a.report()["skipped_budget"] == 1 and a.report()["calls"] == 0
    a2 = GrokSignalAnalyst(analyst_fn=lambda r: {"summary": "y"})
    a2.refresh()
    restored = GrokSignalAnalyst(analyst_fn=lambda r: None)
    restored.load_state(a2.to_state())
    assert restored.report()["last_note"]["summary"] == "y"


# ------------------------------- engine: observe-only + cannot bypass gate ----------------- #
class _FakePredictor:
    """Stand-in for GrokSignalPredictor with a deterministic strong UP prediction (no network)."""
    def __init__(self):
        self._r = {}
        self.scored = []

    def request(self, event_id, context):
        self._r[event_id] = {"p_up": 0.95, "ts": 0.0}

    def get(self, event_id):
        return self._r.get(event_id)

    def score(self, event_id, outcome_up):
        self.scored.append((event_id, bool(outcome_up)))

    def report(self):
        return {"enabled": True, "observe_only": True, "affects_trading": False}

    def to_state(self):
        return {}


class _Mkt:
    def __init__(self, w, *, deep):
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
    t0 = 9_960_000.0
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
                      tradingview_allowed_symbols=("BTC/USD", "BTCUSD"),
                      tradingview_signal_horizon_s=20.0, data_dir=str(tmp_path))
    eng = PulseEngine(cfg, market_feed=_Mkt(win, deep=deep), price_feed=feed)
    eng.grok_predictor = _FakePredictor()         # inject deterministic predictor (no network)
    return eng, t0


def test_engine_grok_pred_observe_only_attached(tmp_path):
    eng, t0 = _engine(tmp_path, deep=True)
    eng.tradingview.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTC/USD",
                                       "direction": "UP", "event_id": "g1"}).encode(), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(4):
        eng.tick(now=t0 + 2 + k * 5)
    # Grok P(up) is attached to candidates as an OBSERVE-ONLY external feature
    ext = [r.get("external") for r in eng.status()["recent_evaluations"] if r.get("external")]
    assert ext and ext[0].get("grok_p_up") == 0.95
    # advance past the forward horizon -> Grok prediction is scored against the realized move
    for k in range(6):
        eng.tick(now=t0 + 40 + k * 5)
    assert eng.grok_predictor.scored                   # B was graded leakage-free
    assert eng.status()["grok_signal_intel"]["observe_only"] is True


def test_engine_grok_cannot_bypass_execution_gate(tmp_path):
    eng, t0 = _engine(tmp_path, deep=False)            # thin book -> gate must reject
    eng.tradingview.ingest(json.dumps({"secret": SECRET, "bot_name": "hermes", "symbol": "BTC/USD",
                                       "direction": "UP", "event_id": "g2"}).encode(), now=t0 - 6)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    assert eng.ledger.trades == 0                       # strong Grok P(up)=0.95 cannot force a trade
    assert eng.ledger.exec_gate_stats()["rejected"]["partial_fill_risk"] >= 1
    assert eng.status()["live_trading_enabled"] is False
