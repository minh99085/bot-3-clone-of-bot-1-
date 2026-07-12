"""GS Quant-style decision lifecycle: structured records, reconciliation, report wiring."""

from __future__ import annotations

from engine.pulse.decisions import (MarketContext, CandidateDecision, ExecutionCostEstimate,
                                     TradeAction, RejectAction, PaperFill, DecisionResult,
                                     LifecycleReconciler, ttc_bucket, half_life_bucket)
from engine.pulse.execution_gate import evaluate_execution, MISSING_MARKET_DATA, REASONS
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


def _mc():
    return MarketContext(event_id="e", market_id="m", title="BTC Up or Down", ttc_s=200.0)


def test_lifecycle_reconciler_balances_all_terminals():
    rc = LifecycleReconciler()
    dr1 = DecisionResult(_mc(), CandidateDecision("up", 0.6, 0.6, 0.1, True, "trade"),
                         features={}, cost=ExecutionCostEstimate(True, "accepted"),
                         action=TradeAction(side="up"), fill=PaperFill("e", "up", 0.5, 10, 5))
    dr1.finalize("accepted")
    dr2 = DecisionResult(_mc(), CandidateDecision("up", 0.5, 0.5, 0.0, False, "edge_below_min"),
                         features={})
    dr2.finalize("rejected", reason="edge_below_min", stage="directional")
    dr3 = DecisionResult(_mc(), CandidateDecision("up", 0.6, 0.6, 0.1, True, "trade"),
                         features={}, cost=ExecutionCostEstimate(False, "wide_spread"))
    dr3.finalize("rejected", reason="wide_spread", stage="execution_gate")
    dr4 = DecisionResult(_mc(), CandidateDecision(None, None, None, 0.0, False, "pending"))
    dr4.finalize("skipped", reason="untrusted_vol")
    dr5 = DecisionResult(_mc(), CandidateDecision(None, None, None, 0.0, False, "pending"))
    dr5.finalize("missing_data", reason="no_open_snapshot")
    dr6 = DecisionResult(_mc(), CandidateDecision(None, None, None, 0.0, False, "pending"))
    dr6.finalize("expired", reason="window_closed")
    for dr in (dr1, dr2, dr3, dr4, dr5, dr6):
        rc.record(dr)
    r = rc.report()
    assert r["created"] == 6 and r["reported"] == 6
    assert r["terminals"] == {"accepted": 1, "rejected": 2, "skipped": 1, "expired": 1,
                              "missing_data": 1}
    assert r["created"] == sum(r["terminals"].values())     # no candidate disappeared
    assert r["rejected_by_stage"]["directional"] == 1 and r["rejected_by_stage"]["execution_gate"] == 1
    assert r["skipped_by_reason"]["untrusted_vol"] == 1
    assert r["missing_by_reason"]["no_open_snapshot"] == 1
    assert r["ledgered"] == 1
    assert r["no_candidate_disappeared"] is True and r["reconciled"] is True


def test_decision_result_to_dict_has_full_lifecycle():
    dr = DecisionResult(_mc(), CandidateDecision("up", 0.6, 0.6, 0.1, True, "trade"))
    dr.mark("feature_scored")
    d = dr.to_dict()
    assert set(d) >= {"market_context", "candidate", "features", "cost", "action", "fill",
                      "status", "reject_stage", "lifecycle"}
    assert d["market_context"]["ttc_bucket"] == "120-240s"


def test_missing_market_data_reason():
    assert MISSING_MARKET_DATA in REASONS
    # no book at all
    assert evaluate_execution(side="up", book=None, outcome_prob=0.9, size_usd=10.0,
                              tick_size=0.01, ttc_s=120.0).reason == MISSING_MARKET_DATA
    # book present but empty asks
    empty = OrderBook(best_bid=0.49, best_ask=None, asks=[], bids=[(0.49, 10.0)])
    assert evaluate_execution(side="up", book=empty, outcome_prob=0.9, size_usd=10.0,
                              tick_size=0.01, ttc_s=120.0).reason == MISSING_MARKET_DATA


def test_ttc_and_half_life_buckets():
    assert ttc_bucket(30) == "<60s" and ttc_bucket(90) == "60-120s"
    assert ttc_bucket(200) == "120-240s" and ttc_bucket(300) == ">=240s" and ttc_bucket(None) == "na"
    assert half_life_bucket(10) == "<30s" and half_life_bucket(60) == "30-120s"
    assert half_life_bucket(200) == ">=120s" and half_life_bucket(None) == "na"


# --- engine-level: every candidate reconciles + report reconciles with ledger ----------- #
class _Mkt:
    def __init__(self, w):
        self._w = w
    def active_windows(self, now=None, **kw):
        return [self._w]
    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=550, bid_depth_usd=500,
                              asks=[(0.55, 1000.0)], bids=[(0.50, 1000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=490, bid_depth_usd=440,
                                asks=[(0.49, 1000.0)], bids=[(0.44, 1000.0)])
        return w
    def fetch_resolution(self, market_id):
        return True


def _cycle(tmp_path):
    t0 = 9_100_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    eng = PulseEngine(PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False,
                                  basis_buffer=0.0, min_seconds_since_open=0.0,
                                  sigma_trust_floor=0.0, min_vol_samples=2, settle_grace_s=0.0,
                                  exec_max_depth_consume_frac=0.9, data_dir=str(tmp_path)),
                      market_feed=_Mkt(win), price_feed=feed)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(8):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)            # settle
    return eng


def test_engine_lifecycle_reconciles_with_ledger(tmp_path):
    eng = _cycle(tmp_path)
    st = eng.status()
    lc = st["decision_lifecycle"]
    eg = st["execution_gate"]
    assert lc["reconciled"] is True and lc["no_candidate_disappeared"] is True
    acc = lc["terminals"]["accepted"]
    assert lc["created"] == sum(lc["terminals"].values())           # no candidate disappeared
    assert lc["ledgered"] == acc
    # cross-report reconciliation: lifecycle accepted == gate accepted == ledger fills/trades
    assert acc == eg["accepted"] == eg["fills"] == eng.ledger.trades >= 1
    # candidate detail captured as structured DecisionResults with a terminal each
    re = st["recent_evaluations"]
    assert re and all({"market_context", "candidate", "lifecycle", "status", "terminal"} <= set(r)
                      for r in re)
    assert all(r["terminal"] in LifecycleReconciler.TERMINALS for r in re)
    acc_row = [r for r in re if r["terminal"] == "accepted"][0]
    assert "execution_costed" in acc_row["lifecycle"] and "ledgered" in acc_row["lifecycle"]
    assert acc_row["features"]["observe_only"] is True              # observe-only confirmed


def test_report_has_all_required_summaries(tmp_path):
    rf = _cycle(tmp_path).status()["research_features"]
    for fld in ("pnl_by_regime", "pnl_by_zscore_bucket", "pnl_by_half_life_bucket",
                "pnl_by_ttc_bucket", "coverage", "missing_data_reasons"):
        assert fld in rf, fld
    assert rf["observe_only"] is True and rf["affects_trading"] is False
