"""Real paper-ledger corpus loader + honest report (Task 2 pivot + Task 8).

Uses the committed VPS trades.json bundle plus small synthetic ledgers.
The report must (a) compute after-cost stats, (b) recover/attach model q,
(c) refuse to call a tiny sample evidence of edge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtest import paper_ledger as pl

REPO = Path(__file__).parent.parent
BUNDLE = REPO / "reports" / "full_trading_report_20260716" / "trades.json"


def test_parse_slug_and_cex_notes():
    assert pl.parse_slug_window("btc-updown-5m-1784204100") == ("btc", "5m", 1784204100)
    assert pl.parse_slug_window("eth-updown-15m-1784205900") == ("eth", "15m", 1784205900)
    assert pl.parse_slug_window("will-biden-win") is None
    e, x = pl.parse_cex_notes("settle_cex asset=BTC entry_cex=64104.7500 exit_cex=64098.2500 z")
    assert e == pytest.approx(64104.75) and x == pytest.approx(64098.25)


def test_outcome_and_q_recovery():
    # direction UP, lost → outcome DOWN; q_up = entry + edge
    t = pl.RealTrade(
        slug="btc-updown-5m-1784204100", asset="btc", timeframe="5m",
        window_ts=1784204100, settled_at="t", direction="UP", p_side=0.24,
        won=False, pnl_usd=-60.0, size_usd=60.0,
        q_up=pl._q_up_from_edge("UP", 0.24, 0.28),
    )
    assert t.outcome_up is False
    assert t.p_up == pytest.approx(0.24)
    assert t.q_up == pytest.approx(0.52)
    # DOWN winner → outcome DOWN; p_up mirrors
    t2 = pl.RealTrade(
        slug="btc-updown-15m-1784205900", asset="btc", timeframe="15m",
        window_ts=1784205900, settled_at="t", direction="DOWN", p_side=0.18,
        won=True, pnl_usd=213.0, size_usd=60.0,
        q_up=pl._q_up_from_edge("DOWN", 0.18, 0.32),
    )
    assert t2.outcome_up is False
    assert t2.p_up == pytest.approx(0.82)
    assert t2.q_up == pytest.approx(1.0 - (0.18 + 0.32))


def test_load_committed_vps_bundle():
    assert BUNDLE.is_file(), "committed VPS trades.json bundle missing"
    trades = pl.load_trades([BUNDLE])
    assert len(trades) >= 5
    assert all(t.asset in ("btc", "eth", "sol") for t in trades)
    assert all(t.q_up is not None for t in trades)  # recovered from enhanced_edge
    # entry_cex/exit_cex parsed from notes
    assert any(t.entry_cex and t.exit_cex for t in trades)


def test_report_flags_insufficient_n():
    trades = pl.load_trades([BUNDLE])
    rep = pl.build_real_report(trades)
    assert rep.n_trades == len(trades)
    assert not rep.sufficient_n
    assert "INSUFFICIENT" in rep.text().upper()
    # Wilson interval must be wide at tiny n
    assert (rep.wilson_hi - rep.wilson_lo) > 0.25


def test_report_after_cost_math(tmp_path):
    # Hand-built ledger: 3 wins @ p=0.5 (+100 each), 2 losses (-100 each)
    ledger = tmp_path / "inst" / "trade_ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    rows = []
    for i, (won, pnl) in enumerate([(True, 100), (True, 100), (True, 100), (False, -100), (False, -100)]):
        rows.append({
            "event": "settlement",
            "slug": f"btc-updown-5m-{1784204100 + i * 300}",
            "direction": "UP", "entry_price": 0.5, "won": won,
            "pnl_usd": pnl, "size_usd": 100.0, "enhanced_edge": 0.1,
            "instance_id": "btc5",
            "notes": "settle_cex asset=BTC entry_cex=100.0 exit_cex=101.0",
        })
    ledger.write_text("\n".join(json.dumps(r) for r in rows))
    trades = pl.load_trades([ledger])
    rep = pl.build_real_report(trades, bankroll=2000.0)
    assert rep.n_trades == 5 and rep.n_wins == 3
    assert rep.win_rate == pytest.approx(0.6)
    assert rep.profit_factor == pytest.approx(300 / 200)
    assert rep.total_pnl == pytest.approx(100.0)
    assert rep.expectancy_usd == pytest.approx(20.0)


def test_report_calibration_when_q_present(tmp_path):
    # 40 trades with q present so calibration path runs
    ledger = tmp_path / "inst" / "trade_ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    rows = []
    for i in range(40):
        won = i % 2 == 0
        rows.append({
            "event": "settlement",
            "slug": f"btc-updown-5m-{1784204100 + i * 300}",
            "direction": "UP", "entry_price": 0.5, "won": won,
            "pnl_usd": 100 if won else -100, "size_usd": 100.0,
            "enhanced_edge": 0.1, "instance_id": "btc5",
            "notes": "x",
        })
    ledger.write_text("\n".join(json.dumps(r) for r in rows))
    trades = pl.load_trades([ledger])
    rep = pl.build_real_report(trades)
    assert rep.n_with_q == 40
    assert rep.brier is not None and 0.0 <= rep.brier <= 1.0
    assert rep.market_brier is not None
    assert rep.calibration_error is not None
    assert rep.log_loss is not None


def test_pretrade_q_overrides_edge_reconstruction(tmp_path):
    inst = tmp_path / "btc5"
    inst.mkdir(parents=True)
    (inst / "trade_ledger.jsonl").write_text(json.dumps({
        "event": "settlement", "signal_id": "sig1",
        "slug": "btc-updown-5m-1784204100", "direction": "UP",
        "entry_price": 0.4, "won": True, "pnl_usd": 150, "size_usd": 100.0,
        "enhanced_edge": 0.2, "notes": "x",
    }))
    (inst / "pretrade_decisions.jsonl").write_text(json.dumps({
        "event": "pretrade", "signal_id": "sig1",
        "slug": "btc-updown-5m-1784204100", "q": 0.77,
    }))
    trades = pl.load_trades(
        pl.default_ledger_paths(tmp_path),
        pretrade_paths=pl.default_pretrade_paths(tmp_path),
    )
    assert len(trades) == 1
    assert trades[0].q_up == pytest.approx(0.77)  # pretrade wins over edge (0.6)
