"""Performance scoring + Word report + score history."""

from __future__ import annotations

import json
from pathlib import Path

from engine.pulse.performance_scoring import (
    PerformanceScoreHistory,
    compute_report_scores,
    score_trading_performance,
)
from engine.pulse.reporting import build_full_report_md, build_report_sections


def _sample_sections():
    return build_report_sections({
        "global_reconciled": True,
        "capital": {
            "total_on_hand_usd": 537.84,
            "total_return_pct": 7.57,
            "realized_pnl_usd": 3.38,
            "arb_realized_pnl_usd": 34.46,
            "total_realized_pnl_usd": 37.84,
        },
        "ledger": {
            "trades": 29, "settled": 29, "win_rate": 0.6897,
            "win_rate_up": 0.5, "win_rate_down": 0.7895, "profit_factor": 1.0752,
        },
        "arbitrage": {"executed": 4, "settled": 4, "realized_profit_usd": 34.46},
        "reconciliation": {"global_reconciled": True},
        "candidate_lifecycle": {"created": 7000, "terminals": {"accepted": 29}},
        "readiness": {"status": "not_ready"},
        "stop_conditions": {"any_halted": False},
        "loops": {"loops": {"heartbeat": {"role": "automation", "trigger": "tick"}}},
        "tradingview": {
            "tradingview_alerts_valid": 200,
            "edge_vs_5min_outcome": {
                "aligned_bot_win_rate": 0.8889,
                "opposed_bot_win_rate": 0.5556,
                "signal_hit_rate": 0.6667,
                "n_settled_with_signal": 18,
            },
            "mtf_gate": {"enabled": True, "blocked": 46},
            "down_bias_gate": {"enabled": True, "blocked": 501},
        },
        "grok_decider": {"direction_accuracy": 0.54, "view_accuracy": 0.67, "decided": 80,
                         "errors": 2},
        "cex_lead_edge": {"enabled": True, "any_proven": False},
    }, status={"ticks": 100})


def test_scores_in_valid_range():
    sec = _sample_sections()
    sc = compute_report_scores(sec, global_reconciled=True)
    for key in ("trading_performance", "operation", "external_signals"):
        assert 0 <= sc[key]["score"] <= 100
        assert sc[key]["grade"] in ("A+", "A", "B+", "B", "C+", "C", "D", "F")
    assert 0 <= sc["overall"]["score"] <= 100


def test_score_history_records_on_settled_change(tmp_path):
    hist = PerformanceScoreHistory(tmp_path / "btc_pulse_score_history.json")
    sc = compute_report_scores(_sample_sections())
    assert hist.record(sc, ticks=1, settled=10, force=True) is not None
    assert hist.record(sc, ticks=2, settled=10) is None  # deduped
    assert hist.record(sc, ticks=3, settled=11, force=True) is not None
    assert len(hist.entries()) == 2
    raw = json.loads((tmp_path / "btc_pulse_score_history.json").read_text(encoding="utf-8"))
    assert raw["schema"] == "btc_pulse_score_history/1.0"


def test_word_report_writes_docx(tmp_path):
    pytest = __import__("pytest")
    docx = pytest.importorskip("docx")
    del docx
    from engine.pulse.word_report import build_word_report
    sec = _sample_sections()
    sc = compute_report_scores(sec)
    light = {"global_reconciled": True, "sections": sec, "scores": sc,
             "score_history": {"entries": []}}
    out = tmp_path / "report.docx"
    data = build_word_report(light, status={"ticks": 5}, ledger={"positions": []},
                             output_path=out)
    assert len(data) > 2000
    assert out.exists()
    assert out.read_bytes()[:2] == b"PK"


def test_markdown_includes_scorecard():
    sec = _sample_sections()
    sc = compute_report_scores(sec)
    light = {"global_reconciled": True, "sections": sec, "scores": sc,
             "score_history": {"entries": [{
                 "utc": "2026-06-25", "settled": 29,
                 "scores": {"trading_performance": 70, "operation": 80,
                            "external_signals": 65, "overall": 72},
             }]}}
    md = build_full_report_md(light, {"ticks": 10}, {})
    assert "Performance Scorecard" in md
    assert "Score history" in md


def test_up_bleed_lowers_trading_score():
    good = score_trading_performance({
        "headline": {"win_rate": 0.7, "win_rate_up": 0.45, "win_rate_down": 0.85,
                     "profit_factor": 1.2, "total_return_pct": 5, "settled": 30,
                     "directional_realized_pnl_usd": 10},
    })
    bad = score_trading_performance({
        "headline": {"win_rate": 0.5, "win_rate_up": 0.5, "win_rate_down": 0.5,
                     "profit_factor": 0.8, "total_return_pct": -5, "settled": 30,
                     "directional_realized_pnl_usd": -20},
    })
    assert good["score"] > bad["score"]