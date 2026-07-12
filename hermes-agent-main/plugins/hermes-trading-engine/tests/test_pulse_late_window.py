"""Late-window high-conviction entry mode (time-decay edge) — gate + observe-only measurement.

Proves: conviction math/buckets; the gate restricts to late+high-conviction (restrict-only) and is
OFF by default; the edge measurement classifies cohorts and computes a verdict; engine end-to-end
the gate blocks early/low-conviction entries, the measurement runs whether or not the gate is on,
and reconciliation + paper-only still hold.
"""

from __future__ import annotations

from engine.pulse.late_window import (conviction, conviction_bucket, LateWindowEntry, LateWindowEdge)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


# ------------------------------- conviction math ------------------------------------------ #
def test_conviction_and_buckets():
    assert conviction(0.5) == 0.0
    assert conviction(0.75) == 0.5 and conviction(0.25) == 0.5
    assert conviction(1.0) == 1.0
    assert conviction(None) is None
    assert conviction_bucket(0.5) == "<0.2"
    assert conviction_bucket(0.75) == "0.4-0.6"
    assert conviction_bucket(0.95) == ">=0.8"
    assert conviction_bucket(None) == "na"


# ------------------------------- gate restrict-only --------------------------------------- #
def test_gate_disabled_passes_everything():
    g = LateWindowEntry(enabled=False)
    r = g.evaluate(ttc_s=300, p_up=0.51)
    assert r["decision"] == "pass" and g.passed == 0 and g.rejected == 0


def test_gate_requires_late_and_high_conviction():
    g = LateWindowEntry(enabled=True, max_ttc_s=120.0, min_conviction=0.40)
    # late + high conviction -> pass
    assert g.evaluate(ttc_s=90, p_up=0.75)["decision"] == "pass"
    # late but low conviction -> reject
    r1 = g.evaluate(ttc_s=90, p_up=0.55)
    assert r1["decision"] == "reject" and r1["reason"] == "lw_low_conviction"
    # early window (not late) -> reject regardless of conviction
    r2 = g.evaluate(ttc_s=250, p_up=0.90)
    assert r2["decision"] == "reject" and r2["reason"] == "lw_not_late"
    rep = g.report()
    assert rep["passed"] == 1 and rep["rejected"] == 2
    assert rep["reject_reasons"]["lw_low_conviction"] == 1
    assert rep["reject_reasons"]["lw_not_late"] == 1


# ------------------------------- edge measurement ----------------------------------------- #
def test_edge_measurement_cohorts_and_verdict():
    e = LateWindowEdge(max_ttc_s=120.0, min_conviction=0.40)
    # late+high-conviction cohort wins a lot
    for _ in range(25):
        e.record_settled(ttc_s=80, p_up=0.80, won=True, pnl=2.0, ev_after_cost=0.05,
                         entry_mode="late_window")
    # other cohort loses
    for _ in range(25):
        e.record_settled(ttc_s=260, p_up=0.55, won=False, pnl=-5.0, ev_after_cost=-0.03,
                         entry_mode="standard")
    rep = e.report()
    assert rep["cohort_late_high_conviction"]["n"] == 25
    assert rep["cohort_late_high_conviction"]["win_rate"] == 1.0
    assert rep["cohort_other"]["win_rate"] == 0.0
    assert rep["verdict"] == "time_decay_edge_present"
    assert "0.6-0.8" in rep["by_conviction_bucket"]
    assert rep["by_entry_mode"]["late_window"]["n"] == 25


def test_edge_state_roundtrip():
    e = LateWindowEdge()
    e.record_settled(ttc_s=80, p_up=0.8, won=True, pnl=2.0)
    e2 = LateWindowEdge()
    e2.load_state(e.to_state())
    assert e2.cohorts == e.cohorts and e2.by_conviction == e.by_conviction


# ============================ engine end-to-end =========================================== #
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


def _engine(tmp_path, **over):
    t0 = 9_970_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False, data_dir=str(tmp_path), **over)
    return PulseEngine(cfg, market_feed=_Mkt(win), price_feed=feed), t0


def _drive(eng, t0):
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)


def test_engine_late_window_gate_blocks_early_entries(tmp_path):
    # every driven tick has ttc>=240 (>max_ttc 120) -> gate rejects all as not-late
    eng, t0 = _engine(tmp_path, late_window_entry_enabled=True, late_window_max_ttc_s=120.0,
                      late_window_min_conviction=0.10)
    _drive(eng, t0)
    assert eng.ledger.trades == 0
    lc = eng.status()["decision_lifecycle"]
    assert lc["rejected_by_stage"].get("late_window_gate", 0) >= 1
    g = eng.status()["late_window_entry"]["gate"]
    assert g["enabled"] is True and g["rejected"] >= 1
    assert g["reject_reasons"].get("lw_not_late", 0) >= 1
    assert eng.light_report()["global_reconciled"] is True
    assert eng.status()["live_trading_enabled"] is False and eng.status()["paper_only"] is True


def test_engine_measurement_runs_with_gate_off(tmp_path):
    # gate OFF -> trades happen and the edge measurement still grades them (observe-only)
    eng, t0 = _engine(tmp_path)                            # late_window_entry_enabled default False
    _drive(eng, t0)
    assert eng.ledger.trades >= 1
    lw = eng.status()["late_window_entry"]
    assert lw["gate"]["enabled"] is False
    em = lw["edge_measurement"]
    total = (em["cohort_late_high_conviction"]["n"] + em["cohort_other"]["n"])
    assert total == sum(1 for p in eng.ledger.positions.values() if p.status == "settled") >= 1
    # conviction bucket appears in the grouped settled report
    assert "pnl_by_conviction_bucket" in eng.light_report()
    assert eng.light_report()["global_reconciled"] is True
