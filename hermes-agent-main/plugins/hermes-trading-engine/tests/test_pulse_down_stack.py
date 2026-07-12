"""DOWN stack composite grader (observe-only)."""

from __future__ import annotations

from engine.pulse.down_stack import DownStackGrader, classify_down_stack
from engine.pulse.promotion import can_promote_proven_edge


def test_classify_composite():
    assert classify_down_stack(
        mtf_alignment="bearish_aligned", stale_divergence="stale_polymarket_down", ttc_s=120.0
    ) == "bearish_stale_late"
    assert classify_down_stack(mtf_alignment="bearish_aligned") == "bearish_only"


def test_grader_proven_bucket():
    g = DownStackGrader(min_samples=5, edge_margin=0.04)
    for _ in range(6):
        g.record(bucket="bearish_stale_late", won=True, pnl=2.0, entry_price=0.55)
    rep = g.report()
    row = next(r for r in rep["buckets"] if r["bucket"] == "bearish_stale_late")
    assert row["n"] == 6
    assert row["proven"] is True
    assert rep["any_proven"] is True


def test_can_promote_proven_edge_gate():
    ok, reasons = can_promote_proven_edge(
        n=40, min_samples=30, wilson_lower=0.62, breakeven_wr=0.55, edge_margin=0.04,
        model_brier=0.24, market_brier=0.25)
    assert ok is True and reasons == []
    bad, reasons2 = can_promote_proven_edge(
        n=40, min_samples=30, wilson_lower=0.56, breakeven_wr=0.55, edge_margin=0.04)
    assert bad is False and "wilson_below_breakeven_margin" in reasons2