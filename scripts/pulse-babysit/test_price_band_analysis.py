#!/usr/bin/env python3
"""Unit tests for 24h DOWN price-band WR analysis."""
from __future__ import annotations

import sys
from pathlib import Path

BABYSIT = Path(__file__).resolve().parent
sys.path.insert(0, str(BABYSIT))

from price_band_analysis import analyze_price_bands, detect_band_issues  # noqa: E402

NOW = 1_800_000_000.0
POLICY = {
    "bands": {
        "cheap": {"max_exclusive": 0.45},
        "sweet": {"min_inclusive": 0.45, "max_inclusive": 0.55},
        "expensive": {"min_exclusive": 0.55},
    },
    "triggers": {
        "cheap_down_bleed": {
            "issue_code": "cheap_down_bleed",
            "min_samples": 2,
            "max_win_rate": 0.40,
        },
        "expensive_down_bleed": {
            "issue_code": "expensive_down_bleed",
            "min_samples": 3,
            "max_win_rate": 0.58,
            "max_pnl_usd": 0,
        },
        "sweet_spot_underuse": {
            "issue_code": "sweet_spot_underuse",
            "min_sweet_wr": 0.70,
            "min_sweet_samples": 3,
            "max_sweet_share": 0.50,
        },
    },
}


def _pos(entry_price: float, won: bool, entry_ts: float = NOW - 3600) -> dict:
    return {
        "side": "down",
        "status": "settled",
        "entry_price": entry_price,
        "entry_ts": entry_ts,
        "won": won,
        "pnl_usd": 4.0 if won else -5.0,
    }


def test_cheap_band_bleed_detected():
    ledger = {"positions": [
        _pos(0.42, False),
        _pos(0.44, False),
        _pos(0.50, True),
        _pos(0.52, True),
    ]}
    analysis = analyze_price_bands(ledger, now_ts=NOW, policy=POLICY)
    assert analysis["bands"]["cheap"]["n"] == 2
    assert analysis["bands"]["cheap"]["win_rate"] == 0.0
    issues = detect_band_issues(analysis, POLICY)
    codes = [i["code"] for i in issues]
    assert "cheap_down_bleed" in codes


def test_expensive_band_bleed_detected():
    ledger = {"positions": [
        _pos(0.60, False),
        _pos(0.65, False),
        _pos(0.70, True),
        _pos(0.50, True),
        _pos(0.51, True),
    ]}
    analysis = analyze_price_bands(ledger, now_ts=NOW, policy=POLICY)
    assert analysis["bands"]["expensive"]["n"] == 3
    assert analysis["bands"]["expensive"]["win_rate"] < 0.58
    issues = detect_band_issues(analysis, POLICY)
    assert any(i["code"] == "expensive_down_bleed" for i in issues)


def test_ignores_up_and_old_positions():
    ledger = {"positions": [
        {"side": "up", "status": "settled", "entry_price": 0.40, "entry_ts": NOW - 100,
         "won": False, "pnl_usd": -5},
        _pos(0.42, False, entry_ts=NOW - 100_000),
    ]}
    analysis = analyze_price_bands(ledger, now_ts=NOW, policy=POLICY)
    assert analysis["total"]["n"] == 0