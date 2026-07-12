"""Global lifecycle/execution/ledger/report reconciliation (acceptance criteria #1-#8).

Proves: (a) a single canonical decision_id threads candidate -> fill -> position; (b) the report
distinguishes every count; (c) mismatched counts FAIL reconciliation and name the failed check;
(d) the zero-reject diagnostic appears with thresholds + observed ranges; (e) execution-gate
reject reasons are counted when synthetic candidates violate the rules; and (f) a legacy ledger
(trades that predate accounting) still reconciles via an explicit baseline bucket.
"""

from __future__ import annotations

import json

from engine.pulse.reconciliation import (global_reconciliation, capture_baseline, empty_baseline,
                                          GateObservations, repair_accounting_drift,
                                          zero_reject_diagnostic)
from engine.pulse.execution_gate import (evaluate_execution, WIDE_SPREAD, INSUFFICIENT_DEPTH,
                                          NEGATIVE_EV, TOO_CLOSE, PARTIAL_FILL_RISK,
                                          STALE_ORDERBOOK, MISSING_MARKET_DATA, REASONS)
from engine.pulse.executor import PulseLedger
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


# ---- helpers to build consistent lifecycle/gate/ledger dicts ------------------------------- #
def _lifecycle(*, accepted, rejected, skipped=0, expired=0, missing=0,
               ledgered=None, execution_costed=None, rej_directional=None, rej_gate=None):
    created = accepted + rejected + skipped + expired + missing
    ledgered = accepted if ledgered is None else ledgered
    rej_gate = (rejected - rejected) if rej_gate is None else rej_gate
    rej_directional = (rejected - rej_gate) if rej_directional is None else rej_directional
    execution_costed = (accepted + rej_gate) if execution_costed is None else execution_costed
    return {"created": created, "reported": created, "execution_costed": execution_costed,
            "ledgered": ledgered,
            "terminals": {"accepted": accepted, "rejected": rejected, "skipped": skipped,
                          "expired": expired, "missing_data": missing},
            "rejected_by_stage": {"directional": rej_directional, "execution_gate": rej_gate}}


def _gate(*, candidates, accepted, rejected_total):
    return {"candidates": candidates, "accepted": accepted, "fills": accepted,
            "rejected_total": rejected_total, "rejected": {}}


def _ledger(*, trades, settled, open_positions):
    return {"trades": trades, "settled": settled, "open_positions": open_positions}


# ============================ acceptance criterion #2/#3/#8 ================================= #
def test_global_reconciled_true_on_clean_start():
    lc = _lifecycle(accepted=3, rejected=5, skipped=1, expired=1, rej_gate=1)
    eg = _gate(candidates=4, accepted=3, rejected_total=1)
    led = _ledger(trades=3, settled=2, open_positions=1)
    r = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led,
                              baseline=empty_baseline())
    assert r["global_reconciled"] is True
    assert r["failed_checks"] == []
    c = r["counts"]
    # every required count is distinguished (acceptance criterion #2)
    for k in ("raw_candidates_created", "rejected_before_execution", "sent_to_execution_gate",
              "execution_gate_accepted", "execution_gate_rejected", "paper_fills_created",
              "ledger_trades", "settled_trades", "open_positions",
              "legacy_trades_before_accounting"):
        assert k in c, k
    assert c["execution_gate_accepted"] == c["paper_fills_created"] == 3
    assert c["rejected_before_execution"] == 4 + 1 + 1   # directional + skipped + expired


def test_global_reconciled_true_with_legacy_baseline():
    base = {"captured": True, "trades": 139, "settled": 137, "open_positions": 2,
            "exec_candidates": 94, "exec_accepted": 94, "exec_rejected_total": 0}
    lc = _lifecycle(accepted=5, rejected=13, skipped=2, rej_gate=1)
    eg = _gate(candidates=94 + 6, accepted=94 + 5, rejected_total=1)
    led = _ledger(trades=139 + 5, settled=140, open_positions=4)
    r = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led, baseline=base)
    assert r["global_reconciled"] is True
    assert r["counts"]["legacy_trades_before_accounting"] == 139
    assert r["counts"]["ledger_trades"] == 144


def test_mismatched_ledger_trades_fails():
    lc = _lifecycle(accepted=3, rejected=5, rej_gate=0)
    eg = _gate(candidates=3, accepted=3, rejected_total=0)
    led = _ledger(trades=99, settled=2, open_positions=1)   # 99 != baseline(0)+fills(3)
    r = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led,
                              baseline=empty_baseline())
    assert r["global_reconciled"] is False
    assert "ledger_trades_explained" in r["failed_checks"]


def test_mismatched_gate_flow_fails():
    lc = _lifecycle(accepted=3, rejected=5, rej_gate=1)
    eg = _gate(candidates=10, accepted=8, rejected_total=2)   # not explained by accounted deltas
    led = _ledger(trades=3, settled=2, open_positions=1)
    r = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led,
                              baseline=empty_baseline())
    assert r["global_reconciled"] is False
    assert "gate_flow_matches_ledger" in r["failed_checks"]


def test_positions_dont_balance_fails():
    lc = _lifecycle(accepted=3, rejected=5, rej_gate=0)
    eg = _gate(candidates=3, accepted=3, rejected_total=0)
    led = _ledger(trades=3, settled=5, open_positions=1)     # 5+1 != 3
    r = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led,
                              baseline=empty_baseline())
    assert r["global_reconciled"] is False
    assert "positions_balance" in r["failed_checks"]


def test_accepted_not_equal_fills_fails():
    lc = _lifecycle(accepted=3, rejected=5, rej_gate=0, ledgered=2)   # a fill went missing
    eg = _gate(candidates=3, accepted=3, rejected_total=0)
    led = _ledger(trades=2, settled=1, open_positions=1)
    r = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led,
                              baseline=empty_baseline())
    assert r["global_reconciled"] is False
    assert "accepted_equals_fills" in r["failed_checks"]


def test_repair_accounting_drift_absorbs_one_missing_fill():
    """Ledger/exec_gate can be +1 vs lifecycle after a persistence race; absorb into baseline."""
    lc = _lifecycle(accepted=86, rejected=5, rej_gate=1, ledgered=86, execution_costed=242)
    eg = _gate(candidates=243, accepted=87, rejected_total=156)
    led = _ledger(trades=87, settled=87, open_positions=0)
    r0 = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led,
                               baseline=empty_baseline())
    assert r0["global_reconciled"] is False
    base, changed = repair_accounting_drift(lifecycle=lc, exec_gate=eg, ledger_stats=led,
                                            baseline=empty_baseline())
    assert changed is True
    assert base["trades"] == 1 and base["exec_accepted"] == 1
    r1 = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led, baseline=base)
    assert r1["global_reconciled"] is True


def test_repair_accounting_drift_absorbs_two_on_existing_baseline():
    """Incremental drift atop an already-patched baseline (live VPS pattern)."""
    base = {"captured": True, "trades": 5, "settled": 5, "open_positions": 0,
            "exec_candidates": 5, "exec_accepted": 5, "exec_rejected_total": 0,
            "note": "absorbed 5 fill(s) missing from lifecycle persistence"}
    lc = _lifecycle(accepted=86, rejected=5, rej_gate=1, ledgered=86, execution_costed=257)
    eg = _gate(candidates=264, accepted=93, rejected_total=171)
    led = _ledger(trades=93, settled=93, open_positions=0)
    r0 = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led, baseline=base)
    assert r0["global_reconciled"] is False
    base2, changed = repair_accounting_drift(lifecycle=lc, exec_gate=eg, ledger_stats=led,
                                             baseline=base)
    assert changed is True
    assert base2["trades"] == 7 and base2["exec_accepted"] == 7
    r1 = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led, baseline=base2)
    assert r1["global_reconciled"] is True


def test_lifecycle_internal_disappearance_fails():
    lc = _lifecycle(accepted=3, rejected=5, rej_gate=0)
    lc["created"] = 99                                       # a candidate vanished
    eg = _gate(candidates=3, accepted=3, rejected_total=0)
    led = _ledger(trades=3, settled=2, open_positions=1)
    r = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=led,
                              baseline=empty_baseline())
    assert r["global_reconciled"] is False
    assert "lifecycle_internal" in r["failed_checks"]


# ============================ acceptance criterion #4 ====================================== #
def test_zero_reject_diagnostic_present_when_no_rejections():
    obs = GateObservations()
    for _ in range(5):
        obs.observe(spread=0.005, ask_depth_usd=1500.0, slippage=0.0008,
                    ev_after_slippage=0.04, ttc_s=210.0)
    diag = zero_reject_diagnostic(
        exec_gate=_gate(candidates=5, accepted=5, rejected_total=0),
        thresholds={"size_usd": 5.0, "max_spread": 0.06, "min_depth_usd": 1.0},
        observations=obs.ranges(), rejected_before_execution=700)
    assert diag is not None and diag["active"] is True
    assert diag["thresholds"]["max_spread"] == 0.06
    assert diag["observed_ranges"]["spread"]["max"] == 0.005
    assert diag["observed_ranges"]["ttc_s"]["min"] == 210.0
    assert any("directional" in s for s in diag["likely_explanations"])


def test_zero_reject_diagnostic_absent_when_rejections_exist():
    assert zero_reject_diagnostic(
        exec_gate=_gate(candidates=5, accepted=4, rejected_total=1),
        thresholds={}, observations={}, rejected_before_execution=0) is None
    # also absent when nothing reached the gate
    assert zero_reject_diagnostic(
        exec_gate=_gate(candidates=0, accepted=0, rejected_total=0),
        thresholds={}, observations={}, rejected_before_execution=0) is None


def test_gate_observations_persist_round_trip():
    obs = GateObservations()
    obs.observe(spread=0.01, ask_depth_usd=500.0, slippage=0.001, ev_after_slippage=0.02,
                ttc_s=120.0)
    obs.observe(spread=0.03, ask_depth_usd=900.0, slippage=0.004, ev_after_slippage=0.05,
                ttc_s=240.0)
    obs2 = GateObservations()
    obs2.load_state(obs.to_state())
    assert obs2.n == 2
    assert obs2.ranges()["spread"] == {"min": 0.01, "max": 0.03, "mean": 0.02, "n": 2}


# ============================ acceptance criterion #6 ====================================== #
def test_execution_gate_reject_reasons_counted():
    """Synthetic candidates that violate spread/depth/slippage/time/partial/stale/missing rules
    are each counted under their explicit reason, and the gate reconciles (candidates ==
    accepted + rejected)."""
    led = PulseLedger()

    def run(book, **kw):
        ex = evaluate_execution(side="up", book=book, outcome_prob=kw.pop("p", 0.9),
                                size_usd=kw.pop("size", 5.0), tick_size=0.01,
                                ttc_s=kw.pop("ttc", 120.0), **kw)
        led.record_exec(ex.accepted, ex.reason)
        return ex.reason

    # wide spread
    assert run(OrderBook(best_bid=0.30, best_ask=0.90, ask_depth_usd=900,
                         asks=[(0.90, 1000.0)], bids=[(0.30, 1000.0)])) == WIDE_SPREAD
    # insufficient depth
    assert run(OrderBook(best_bid=0.50, best_ask=0.51, ask_depth_usd=0.2,
                         asks=[(0.51, 0.4)], bids=[(0.50, 100.0)])) == INSUFFICIENT_DEPTH
    # partial-fill risk (thin ladder cannot fully fill the order)
    assert run(OrderBook(best_bid=0.50, best_ask=0.51, ask_depth_usd=2.0,
                         asks=[(0.51, 1.0)], bids=[(0.50, 100.0)]),
               size=50.0) == PARTIAL_FILL_RISK
    # negative EV after slippage (price above the outcome prob)
    assert run(OrderBook(best_bid=0.85, best_ask=0.88, ask_depth_usd=5000,
                         asks=[(0.88, 100000.0)], bids=[(0.85, 100000.0)]),
               p=0.80) == NEGATIVE_EV
    # too close to resolution
    assert run(OrderBook(best_bid=0.50, best_ask=0.51, ask_depth_usd=900,
                         asks=[(0.51, 1000.0)], bids=[(0.50, 1000.0)]), ttc=1.0) == TOO_CLOSE
    # stale orderbook
    assert run(OrderBook(best_bid=0.50, best_ask=0.51, ask_depth_usd=900, ts=1000.0,
                         asks=[(0.51, 1000.0)], bids=[(0.50, 1000.0)]),
               now=99999.0, max_book_age_s=30.0) == STALE_ORDERBOOK
    # missing market data
    assert run(None) == MISSING_MARKET_DATA
    # one acceptance
    assert run(OrderBook(best_bid=0.50, best_ask=0.51, ask_depth_usd=5000,
                         asks=[(0.51, 100000.0)], bids=[(0.50, 100000.0)]), p=0.95) == "accepted"

    eg = led.exec_gate_stats()
    assert eg["candidates"] == 8 and eg["accepted"] == 1
    assert eg["rejected_total"] == 7 and eg["reconciled"] is True
    for reason in (WIDE_SPREAD, INSUFFICIENT_DEPTH, PARTIAL_FILL_RISK, NEGATIVE_EV, TOO_CLOSE,
                   STALE_ORDERBOOK, MISSING_MARKET_DATA):
        assert eg["rejected"][reason] == 1, reason
    assert set(eg["rejected"]) == set(REASONS)


# ============================ acceptance criterion #1 + engine-level ======================= #
class _Mkt:
    def __init__(self, w):
        self._w = w

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=5500, bid_depth_usd=5000,
                              asks=[(0.55, 100000.0)], bids=[(0.50, 100000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=4900, bid_depth_usd=4400,
                                asks=[(0.49, 100000.0)], bids=[(0.44, 100000.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _run_cycle(cfg, mkt):
    t0 = 9_700_000.0
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    eng = PulseEngine(cfg, market_feed=mkt, price_feed=feed)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(8):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)
    return eng


def _cfg(tmp_path):
    return PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False,
                       min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                       settle_grace_s=0.0, exec_max_depth_consume_frac=0.9, data_dir=str(tmp_path))


def test_engine_clean_start_global_reconciled_and_decision_id():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                          open_ts=9_700_000.0, close_ts=9_700_000.0 + 300,
                          up_token_id="U", down_token_id="D")
        eng = _run_cycle(_cfg(d), _Mkt(win))
        rep = eng.light_report()
        assert rep["global_reconciled"] is True
        assert not rep["reconciliation"]["failed_checks"]
        # canonical decision_id threads candidate -> fill -> ledger position (criterion #1)
        pos = next(iter(eng.ledger.positions.values()))
        assert pos.decision_id == "e1" == pos.window_key
        acc = [r for r in eng.status()["recent_evaluations"] if r["terminal"] == "accepted"][0]
        assert acc["decision_id"] == "e1"
        assert acc["fill"]["decision_id"] == "e1"
        # zero-reject diagnostic surfaces because the synthetic book is deep+tight
        eg = rep["execution_stats"]
        if eg["candidates"] > 0 and eg["rejected_total"] == 0:
            assert rep["execution_gate_zero_reject_diagnostic"]["active"] is True


def test_engine_reconciles_with_legacy_ledger_baseline():
    """A ledger restored from disk with trades that predate accounting must still reconcile:
    the legacy totals are captured as an explicit baseline bucket and global_reconciled stays
    true after new accounted trades."""
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as d:
        # seed a legacy ledger: 10 trades / 9 settled, with 1 still-open legacy position that
        # never settles during the test (close_ts far in the future). No accounting_state -> the
        # engine must capture the baseline itself.
        legacy = {
            "paper_only": True,
            "stats": {"trades": 10, "settled": 9, "wins": 5, "realized_pnl_usd": 1.5},
            "accumulators": {"settled_entry_sum": 4.5, "side_n": {"up": 5, "down": 4},
                             "side_wins": {"up": 3, "down": 2},
                             "settle_sources": {"polymarket_resolution": 9,
                                                "rtds_chainlink_proxy": 0},
                             "recon": {"both": 0, "agree": 0, "disagree": 0},
                             "exec_candidates": 8, "exec_accepted": 8,
                             "exec_rejected": {}, "gross_win": 5.0, "gross_loss": 3.5,
                             "equity": 1.5, "equity_peak": 2.0, "max_drawdown": 0.5},
            "positions": [{"window_key": "legacy_open", "market_id": "mL", "title": "BTC",
                           "side": "up", "token_id": "U", "entry_price": 0.5, "size_usd": 10.0,
                           "shares": 20.0, "fair_at_entry": 0.5, "edge_at_entry": 0.0,
                           "open_ts": 1.0, "close_ts": 9.9e12, "entry_ts": 1.0, "status": "open"}],
        }
        with open(os.path.join(d, "btc_pulse_ledger.json"), "w") as f:
            json.dump(legacy, f)
        win = PulseWindow(event_id="e2", market_id="m2", slug="s", title="BTC Up or Down",
                          open_ts=9_700_000.0, close_ts=9_700_000.0 + 300,
                          up_token_id="U", down_token_id="D")
        eng = _run_cycle(_cfg(d), _Mkt(win))
        rep = eng.light_report()
        assert rep["global_reconciled"] is True, rep["reconciliation"]["failed_checks"]
        c = rep["reconciliation"]["counts"]
        assert c["legacy_trades_before_accounting"] == 10
        assert c["ledger_trades"] == eng.ledger.trades >= 11   # legacy 10 + >=1 new
        assert c["settled_trades"] + c["open_positions"] == c["ledger_trades"]
        # baseline persisted so a SECOND restart keeps reconciling (no double counting)
        eng2 = PulseEngine(_cfg(d), market_feed=_Mkt(win),
                           price_feed=PulsePriceFeed(fetcher=lambda: 64000.0,
                                                     source_name="rtds_chainlink",
                                                     vol=RollingVol(window_s=900, min_samples=8),
                                                     max_open_lag_s=20.0))
        assert eng2._baseline["trades"] == 10
        assert eng2.light_report()["global_reconciled"] is True
