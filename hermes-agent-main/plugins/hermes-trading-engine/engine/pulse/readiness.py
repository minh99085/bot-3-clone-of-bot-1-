"""Success gates / readiness report for the BTC 5-min pulse — prevents false '80% bot' claims.

Evaluates explicit gates and maps them to a status: not_ready | early_evidence |
serious_evidence | strong_evidence. The bot may NEVER be claimed an 80% bot unless ALL gates
pass (status strong_evidence + ready_to_claim_80pct). Report-only.
"""

from __future__ import annotations

from typing import Optional

STATUSES = ("not_ready", "early_evidence", "serious_evidence", "strong_evidence")


def readiness_report(*, accepted: int, win_rate: Optional[float], net_pnl: Optional[float],
                     profit_factor: Optional[float], calibration_error: Optional[float],
                     max_drawdown: Optional[float], avg_win: Optional[float],
                     avg_loss: Optional[float], reconciliation_ok: bool,
                     missing_settlement: bool, unmodeled_fill: bool, safety_bypass: bool,
                     min_profit_factor: float = 1.5, max_calibration_error: float = 0.10,
                     max_drawdown_limit_usd: float = 100.0,
                     win_rate_target: float = 0.80) -> dict:
    gates = {
        "accepted_ge_100": accepted >= 100,
        "accepted_ge_500": accepted >= 500,
        "accepted_ge_1000": accepted >= 1000,
        "win_rate_ge_80": (win_rate is not None and win_rate >= win_rate_target),
        "positive_net_paper_pnl": (net_pnl is not None and net_pnl > 0),
        "profit_factor_ok": (profit_factor is not None and profit_factor >= min_profit_factor),
        "calibration_error_ok": (calibration_error is not None
                                 and calibration_error <= max_calibration_error),
        "max_drawdown_ok": (max_drawdown is not None and max_drawdown <= max_drawdown_limit_usd),
        "loss_size_le_win_size": (avg_loss is None or avg_win is None or avg_loss <= avg_win),
        "no_reconciliation_failures": bool(reconciliation_ok),
        "no_missing_settlement_data": not missing_settlement,
        "no_unmodeled_fill_assumptions": not unmodeled_fill,
        "no_safety_bypass": not safety_bypass,
    }
    clean = (gates["no_reconciliation_failures"] and gates["no_missing_settlement_data"]
             and gates["no_unmodeled_fill_assumptions"] and gates["no_safety_bypass"])
    core = gates["win_rate_ge_80"] and gates["positive_net_paper_pnl"] and clean

    if not clean:
        status = "not_ready"
    elif (gates["accepted_ge_1000"] and core and gates["profit_factor_ok"]
          and gates["calibration_error_ok"] and gates["max_drawdown_ok"]
          and gates["loss_size_le_win_size"]):
        status = "strong_evidence"
    elif gates["accepted_ge_500"] and core and gates["profit_factor_ok"]:
        status = "serious_evidence"
    elif gates["accepted_ge_100"] and core:
        status = "early_evidence"
    else:
        status = "not_ready"

    return {"report_only": True, "status": status,
            "ready_to_claim_80pct": (status == "strong_evidence" and all(gates.values())),
            "gates": gates,
            "metrics": {"accepted": accepted, "win_rate": win_rate, "net_pnl_usd": net_pnl,
                        "profit_factor": profit_factor, "calibration_error": calibration_error,
                        "max_drawdown_usd": max_drawdown, "avg_win_usd": avg_win,
                        "avg_loss_usd": avg_loss}}
