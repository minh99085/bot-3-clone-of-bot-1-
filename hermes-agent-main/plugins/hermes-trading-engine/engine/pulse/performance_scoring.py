"""Performance scoring for the 3-section BTC pulse report + append-only history.

Scores are 0–100 per section with documented sub-components. History is persisted in
``btc_pulse_score_history.json`` so operators can track trends over time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(x)))


def _grade(score: float) -> str:
    s = float(score)
    if s >= 95:
        return "A+"
    if s >= 90:
        return "A"
    if s >= 85:
        return "B+"
    if s >= 80:
        return "B"
    if s >= 75:
        return "C+"
    if s >= 70:
        return "C"
    if s >= 60:
        return "D"
    return "F"


def _weighted(components: list[tuple[str, float, float]]) -> tuple[float, dict]:
    """(name, score, weight) -> (total, breakdown dict)."""
    total_w = sum(w for _, _, w in components) or 1.0
    breakdown = {}
    acc = 0.0
    for name, score, weight in components:
        contrib = _clamp(score) * weight / total_w
        breakdown[name] = {"score": round(_clamp(score), 1), "weight": weight,
                           "contribution": round(contrib, 1)}
        acc += contrib
    return round(acc, 1), breakdown


def score_trading_performance(section: dict, *, global_reconciled: bool = True) -> dict:
    hl = (section or {}).get("headline", {}) or {}
    ret = float(hl.get("total_return_pct") or hl.get("return_pct") or 0.0)
    return_score = _clamp(50.0 + ret * 5.0)

    wr = hl.get("win_rate")
    wr_score = 50.0 if wr is None else _clamp((float(wr) - 0.40) * 200.0)

    pf = hl.get("profit_factor")
    pf_score = 50.0 if pf is None else _clamp((float(pf) - 0.70) * 125.0)

    dir_pnl = float(hl.get("directional_realized_pnl_usd") or 0.0)
    dir_score = _clamp(50.0 + dir_pnl * 2.0)

    wr_up = hl.get("win_rate_up")
    wr_down = hl.get("win_rate_down")
    if wr_down is not None and wr_up is not None:
        side_score = _clamp(50.0 + (float(wr_down) - float(wr_up)) * 100.0
                            + (float(wr_down) - 0.50) * 60.0)
    else:
        side_score = 50.0

    integrity = 100.0 if global_reconciled else 0.0
    settled = int(hl.get("settled") or 0)
    sample_score = _clamp(settled * 3.33) if settled < 30 else 100.0

    total, breakdown = _weighted([
        ("return_pct", return_score, 20),
        ("win_rate", wr_score, 25),
        ("profit_factor", pf_score, 15),
        ("directional_pnl", dir_score, 15),
        ("side_balance", side_score, 10),
        ("accounting_integrity", integrity, 10),
        ("sample_size", sample_score, 5),
    ])
    return {
        "score": total,
        "grade": _grade(total),
        "components": breakdown,
        "note": "Profitability, win-rate, side balance (DOWN>UP), and ledger integrity.",
    }


def score_operation(section: dict, *, global_reconciled: bool = True) -> dict:
    eng = (section or {}).get("engine", {}) or {}
    readiness = (section or {}).get("readiness", {}) or {}
    stops = (section or {}).get("stop_conditions", {}) or {}
    lc = (section or {}).get("candidate_lifecycle", {}) or {}
    gd = (section or {}).get("grok_decider_ops", {}) or {}

    integrity = 100.0 if (eng.get("global_reconciled") or global_reconciled) else 0.0
    ready = readiness.get("status")
    ready_score = 100.0 if ready == "ready" else (70.0 if ready == "warming_up" else 40.0)

    halted = bool(stops.get("any_halted") or stops.get("halted_directional"))
    stop_score = 0.0 if halted else 100.0

    loops = ((section or {}).get("loops", {}) or {}).get("loops", {}) or {}
    loop_score = 100.0 if loops else 30.0
    if loops:
        stale = sum(1 for info in loops.values()
                    if isinstance(info, dict) and info.get("stale"))
        loop_score = _clamp(100.0 - stale * 15.0)

    created = int(lc.get("created") or 0)
    accepted = int((lc.get("terminals") or {}).get("accepted") or 0)
    activity_score = 50.0
    if created > 0:
        activity_score = _clamp(40.0 + min(accepted, 50) * 1.2 + min(created / 100.0, 30.0))

    errors = int(gd.get("errors") or 0)
    decided = max(int(gd.get("decided") or 0), 1)
    error_score = _clamp(100.0 - (errors / decided) * 200.0)

    total, breakdown = _weighted([
        ("accounting_integrity", integrity, 20),
        ("readiness", ready_score, 15),
        ("stop_conditions", stop_score, 15),
        ("loops_healthy", loop_score, 20),
        ("pipeline_activity", activity_score, 15),
        ("low_errors", error_score, 15),
    ])
    return {
        "score": total,
        "grade": _grade(total),
        "components": breakdown,
        "note": "Engine health: reconciliation, loops, stops, and pipeline activity.",
    }


def score_external_signals(section: dict) -> dict:
    imp = (section or {}).get("impact_summary", {}) or {}
    tv = (section or {}).get("tradingview", {}) or {}

    aligned = imp.get("tv_aligned_bot_win_rate")
    opposed = imp.get("tv_opposed_bot_win_rate")
    if aligned is not None and opposed is not None:
        spread = float(aligned) - float(opposed)
        tv_edge_score = _clamp(50.0 + spread * 120.0 + (float(aligned) - 0.55) * 80.0)
    elif aligned is not None:
        tv_edge_score = _clamp((float(aligned) - 0.45) * 150.0)
    else:
        tv_edge_score = 30.0

    hit = imp.get("tv_signal_hit_rate")
    hit_score = 50.0 if hit is None else _clamp((float(hit) - 0.45) * 150.0)

    valid = int(tv.get("tradingview_alerts_valid") or 0)
    flow_score = _clamp(valid / 5.0) if valid < 100 else 100.0

    gd_acc = imp.get("grok_direction_accuracy")
    grok_score = 50.0 if gd_acc is None else _clamp((float(gd_acc) - 0.45) * 150.0)

    gate_score = 50.0
    mg = tv.get("mtf_gate", {}) or {}
    dbg = tv.get("down_bias_gate", {}) or {}
    if mg.get("enabled") or dbg.get("enabled"):
        blocked = int(mg.get("blocked") or 0) + int(dbg.get("blocked") or 0)
        gate_score = _clamp(60.0 + min(blocked, 40) * 1.0)

    cex_proven = bool(imp.get("cex_lead_any_proven"))
    cex_score = 100.0 if cex_proven else 40.0

    total, breakdown = _weighted([
        ("tv_aligned_edge", tv_edge_score, 30),
        ("tv_hit_rate", hit_score, 15),
        ("tv_alert_flow", flow_score, 15),
        ("grok_accuracy", grok_score, 15),
        ("entry_gates_active", gate_score, 15),
        ("cex_lead_proven", cex_score, 10),
    ])
    return {
        "score": total,
        "grade": _grade(total),
        "components": breakdown,
        "note": "External signal quality and how well signals align with bot outcomes.",
    }


def compute_report_scores(sections: dict, *, global_reconciled: bool = True) -> dict:
    sec = sections or {}
    tp = score_trading_performance(sec.get("trading_performance", {}),
                                   global_reconciled=global_reconciled)
    op = score_operation(sec.get("operation", {}), global_reconciled=global_reconciled)
    ex = score_external_signals(sec.get("external_signals", {}))
    overall = round(tp["score"] * 0.50 + op["score"] * 0.25 + ex["score"] * 0.25, 1)
    return {
        "schema": "btc_pulse_report_scores/1.0",
        "weights": {"trading_performance": 0.50, "operation": 0.25, "external_signals": 0.25},
        "trading_performance": tp,
        "operation": op,
        "external_signals": ex,
        "overall": {"score": overall, "grade": _grade(overall)},
    }


class PerformanceScoreHistory:
    """Append-only score history persisted alongside other pulse artifacts."""

    def __init__(self, path: Path, *, max_entries: int = 500):
        self.path = Path(path)
        self.max_entries = max(int(max_entries), 10)
        self._data: dict = {"schema": "btc_pulse_score_history/1.0", "entries": []}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
                self._data = raw
        except Exception:  # noqa: BLE001
            pass

    def entries(self) -> list:
        return list(self._data.get("entries") or [])

    def latest(self) -> Optional[dict]:
        ents = self.entries()
        return ents[-1] if ents else None

    def should_record(self, scores: dict, *, ticks: int, settled: int,
                      min_interval_s: float = 1800.0) -> bool:
        """Record when settled changes, overall score moves ≥1, or interval elapsed."""
        prev = self.latest()
        if prev is None:
            return True
        if int(prev.get("settled") or 0) != int(settled):
            return True
        def _overall_val(scores_dict, key="overall"):
            ov = (scores_dict or {}).get(key)
            if isinstance(ov, dict):
                return ov.get("score")
            return ov

        prev_overall = _overall_val(prev.get("scores"))
        cur_overall = _overall_val(scores)
        if prev_overall is not None and cur_overall is not None:
            if abs(float(cur_overall) - float(prev_overall)) >= 1.0:
                return True
        age = time.time() - float(prev.get("ts") or 0)
        return age >= float(min_interval_s)

    def record(self, scores: dict, *, ticks: int, settled: int,
               force: bool = False) -> Optional[dict]:
        if not force and not self.should_record(scores, ticks=ticks, settled=settled):
            return None
        entry = {
            "ts": time.time(),
            "utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "ticks": int(ticks),
            "settled": int(settled),
            "scores": {
                "trading_performance": (scores.get("trading_performance") or {}).get("score"),
                "operation": (scores.get("operation") or {}).get("score"),
                "external_signals": (scores.get("external_signals") or {}).get("score"),
                "overall": (scores.get("overall") or {}).get("score"),
                "grades": {
                    "trading_performance": (scores.get("trading_performance") or {}).get("grade"),
                    "operation": (scores.get("operation") or {}).get("grade"),
                    "external_signals": (scores.get("external_signals") or {}).get("grade"),
                    "overall": (scores.get("overall") or {}).get("grade"),
                },
            },
        }
        ents = self._data.setdefault("entries", [])
        ents.append(entry)
        if len(ents) > self.max_entries:
            self._data["entries"] = ents[-self.max_entries:]
        self.save()
        return entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=1), encoding="utf-8")

    def to_dict(self) -> dict:
        return dict(self._data)

    def summary_table(self, last_n: int = 30) -> list[dict]:
        rows = []
        for e in self.entries()[-last_n:]:
            sc = e.get("scores") or {}
            rows.append({
                "utc": e.get("utc"),
                "ticks": e.get("ticks"),
                "settled": e.get("settled"),
                "trading_performance": sc.get("trading_performance"),
                "operation": sc.get("operation"),
                "external_signals": sc.get("external_signals"),
                "overall": sc.get("overall"),
            })
        return rows