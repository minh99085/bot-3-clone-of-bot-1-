"""Selective win-rate tier classifier (Phase 8) — report-only."""

from __future__ import annotations

from engine.pulse.tiers import classify_tier, build_tier_table, tier_report, TIERS


def test_tier_aplus_requires_winrate_sample_and_clean():
    aplus = classify_tier(n=120, win_rate=0.83, pnl_usd=50.0, reconciled=True, safety_ok=True)
    assert aplus["tier"] == "A+"
    # same win-rate but small sample -> only A
    a = classify_tier(n=40, win_rate=0.83, pnl_usd=20.0)
    assert a["tier"] == "A"
    # 80%+ but not reconciled -> cannot be A+ (drops to A or B)
    not_clean = classify_tier(n=120, win_rate=0.83, pnl_usd=50.0, reconciled=False)
    assert not_clean["tier"] in ("A", "B")


def test_tier_c_and_d():
    assert classify_tier(n=50, win_rate=0.40, pnl_usd=-10.0)["tier"] == "C"
    assert classify_tier(n=50, win_rate=0.9, pnl_usd=50.0, safety_ok=False)["tier"] == "D"
    assert classify_tier(n=50, win_rate=0.9, pnl_usd=50.0, max_drawdown=0.8,
                         drawdown_limit=0.5)["tier"] == "D"


def test_tier_b_default_on_insufficient_data():
    assert classify_tier(n=0, win_rate=None, pnl_usd=None)["tier"] == "B"
    assert classify_tier(n=10, win_rate=0.65, pnl_usd=5.0)["tier"] == "B"   # below A threshold


def test_tier_report_table_and_census():
    grouped = {"high": {"n": 120, "win_rate": 0.85, "pnl_usd": 60.0},
               "low": {"n": 80, "win_rate": 0.45, "pnl_usd": -20.0}}
    rep = tier_report({"edge_quality": grouped}, reconciled=True, safety_ok=True)
    assert rep["report_only"] is True and rep["affects_trading"] is False
    assert rep["table"]["edge_quality:high"]["tier"] == "A+"
    assert rep["table"]["edge_quality:low"]["tier"] == "C"
    assert rep["tier_census"]["A+"] == 1 and rep["tier_census"]["C"] == 1
    assert set(rep["tier_census"]) == set(TIERS)
