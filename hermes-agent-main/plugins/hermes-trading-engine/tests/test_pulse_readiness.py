"""Phase 13 success gates / readiness report — never claims 80% bot unless all gates pass."""

from __future__ import annotations

from engine.pulse.readiness import readiness_report, STATUSES


def _kw(**over):
    base = dict(accepted=0, win_rate=None, net_pnl=None, profit_factor=None,
                calibration_error=None, max_drawdown=None, avg_win=None, avg_loss=None,
                reconciliation_ok=True, missing_settlement=False, unmodeled_fill=False,
                safety_bypass=False)
    base.update(over)
    return base


def test_not_ready_when_no_data():
    r = readiness_report(**_kw())
    assert r["status"] == "not_ready" and r["ready_to_claim_80pct"] is False


def test_not_ready_on_any_safety_or_reconciliation_failure():
    # even a perfect track record is not_ready if there's a safety bypass / unclean reconciliation
    strong = _kw(accepted=2000, win_rate=0.85, net_pnl=500.0, profit_factor=3.0,
                 calibration_error=0.05, max_drawdown=10.0, avg_win=6.0, avg_loss=5.0)
    assert readiness_report(**{**strong, "safety_bypass": True})["status"] == "not_ready"
    assert readiness_report(**{**strong, "reconciliation_ok": False})["status"] == "not_ready"
    assert readiness_report(**{**strong, "missing_settlement": True})["status"] == "not_ready"
    assert readiness_report(**{**strong, "unmodeled_fill": True})["status"] == "not_ready"


def test_evidence_ladder():
    early = _kw(accepted=120, win_rate=0.82, net_pnl=40.0, profit_factor=1.2,
               calibration_error=0.2, max_drawdown=10.0, avg_win=6.0, avg_loss=5.0)
    assert readiness_report(**early)["status"] == "early_evidence"
    serious = _kw(accepted=600, win_rate=0.82, net_pnl=200.0, profit_factor=1.8,
                  calibration_error=0.2, max_drawdown=40.0, avg_win=6.0, avg_loss=5.0)
    assert readiness_report(**serious)["status"] == "serious_evidence"
    strong = _kw(accepted=1200, win_rate=0.85, net_pnl=500.0, profit_factor=2.5,
                 calibration_error=0.05, max_drawdown=10.0, avg_win=6.0, avg_loss=5.0)
    out = readiness_report(**strong)
    assert out["status"] == "strong_evidence" and out["ready_to_claim_80pct"] is True
    assert all(out["gates"].values())


def test_strong_evidence_requires_all_gates_for_claim():
    # 80% + 1000+ but profit factor too low / drawdown too big -> not strong, not claimable
    near = _kw(accepted=1200, win_rate=0.85, net_pnl=500.0, profit_factor=1.1,
               calibration_error=0.05, max_drawdown=10.0, avg_win=6.0, avg_loss=5.0)
    r = readiness_report(**near)
    assert r["status"] in ("serious_evidence", "early_evidence")
    assert r["ready_to_claim_80pct"] is False
    assert r["status"] in STATUSES
