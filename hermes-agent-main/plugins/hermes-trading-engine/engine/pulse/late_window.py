"""Late-window high-conviction entry mode + time-decay edge measurement (PAPER ONLY).

Hypothesis (the "time-decay edge"): on a 5-min binary up/down market, as seconds-to-close shrink,
a given price displacement implies a probability far from 0.5 (less time left for a reversal). When
the digital model is *both* late in the window *and* highly convicted, the directional call should
win more often — and Polymarket's price often still lags it. The live report already hinted at this
(``ttc 60-120s`` was a winning bucket; ``ttc >= 240s`` lost).

This module provides two pieces:

1. ``LateWindowEntry`` — a RESTRICT-ONLY entry mode (config-gated, default OFF). When enabled it
   only PERMITS trades that are late-window AND high-conviction; everything else is rejected. It can
   only make the bot MORE selective — it never creates, forces, sizes, or fast-tracks a trade, and
   the strict execution gate remains the sole trade authority.

2. ``LateWindowEdge`` — an OBSERVE-ONLY measurement (always on). It classifies every settled trade
   into the ``late_high_conviction`` cohort vs ``other`` using reference thresholds, and buckets
   win-rate / PnL / EV by conviction and time-to-close, so the edge can be GRADED from live trades
   *before* the gate is enabled. Report-only.
"""

from __future__ import annotations

from typing import Optional


def conviction(p_up: Optional[float]) -> Optional[float]:
    """Directional conviction in [0, 1]: 0 == coin-flip (0.5), 1 == certain (0 or 1)."""
    if p_up is None:
        return None
    return round(abs(float(p_up) - 0.5) * 2.0, 4)


def conviction_bucket(p_up: Optional[float]) -> str:
    c = conviction(p_up)
    if c is None:
        return "na"
    if c < 0.2:
        return "<0.2"
    if c < 0.4:
        return "0.2-0.4"
    if c < 0.6:
        return "0.4-0.6"
    if c < 0.8:
        return "0.6-0.8"
    return ">=0.8"


class LateWindowEntry:
    """Restrict-only late-window high-conviction entry gate. ``evaluate`` returns
    ``{"decision": "pass"|"reject", "reason": str|None, ...}``. Disabled -> always pass."""

    def __init__(self, *, enabled: bool = False, max_ttc_s: float = 120.0,
                 min_conviction: float = 0.40):
        self.enabled = bool(enabled)
        self.max_ttc_s = float(max_ttc_s)
        self.min_conviction = float(min_conviction)
        self.passed = 0
        self.rejected = 0
        self.reject_reasons: dict = {}

    def evaluate(self, *, ttc_s: Optional[float], p_up: Optional[float]) -> dict:
        conv = conviction(p_up)
        late = (ttc_s is not None and float(ttc_s) <= self.max_ttc_s)
        high_conv = (conv is not None and conv >= self.min_conviction)
        base = {"conviction": conv, "late": late, "high_conviction": high_conv,
                "active": self.enabled}
        if not self.enabled:
            return {"decision": "pass", "reason": None, **base}
        if late and high_conv:
            self.passed += 1
            return {"decision": "pass", "reason": None, **base}
        reason = "lw_not_late" if not late else "lw_low_conviction"
        self.rejected += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
        return {"decision": "reject", "reason": reason, **base}

    def report(self) -> dict:
        return {
            "enabled": self.enabled, "mode": "restrict_only_late_high_conviction",
            "affects_trading": self.enabled, "can_force_trade": False,
            "execution_gate_still_authoritative": True,
            "max_ttc_s": self.max_ttc_s, "min_conviction": self.min_conviction,
            "passed": self.passed, "rejected": self.rejected,
            "reject_reasons": dict(self.reject_reasons),
            "note": ("when enabled, only late-window AND high-conviction setups may trade; can only "
                     "PREVENT trades, never force/size/bypass the execution gate."),
        }

    def to_state(self) -> dict:
        return {"passed": self.passed, "rejected": self.rejected,
                "reject_reasons": dict(self.reject_reasons)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.passed = int(data.get("passed", 0) or 0)
        self.rejected = int(data.get("rejected", 0) or 0)
        self.reject_reasons = {k: int(v or 0) for k, v in (data.get("reject_reasons") or {}).items()}


class LateWindowEdge:
    """Observe-only: grade whether late-window high-conviction trades win more, from live settled
    trades, bucketed by conviction and time-to-close. Report-only; never affects trading."""

    MIN_COHORT = 20            # min settled trades in a cohort before claiming an edge

    def __init__(self, *, max_ttc_s: float = 120.0, min_conviction: float = 0.40):
        self.max_ttc_s = float(max_ttc_s)
        self.min_conviction = float(min_conviction)
        self.cohorts: dict = {}          # "late_high_conviction"|"other" -> stat
        self.by_conviction: dict = {}    # conviction_bucket -> stat
        self.by_ttc: dict = {}           # ttc_bucket -> stat
        self.by_entry_mode: dict = {}    # entry_mode -> stat

    @staticmethod
    def _stat() -> dict:
        return {"n": 0, "wins": 0, "pnl": 0.0, "ev": 0.0}

    def _bump(self, table: dict, key: str, won: bool, pnl: float, ev: float) -> None:
        s = table.setdefault(str(key), self._stat())
        s["n"] += 1
        s["wins"] += int(bool(won))
        s["pnl"] = round(s["pnl"] + float(pnl), 6)
        s["ev"] = round(s["ev"] + float(ev or 0.0), 6)

    @staticmethod
    def _ttc_bucket(ttc_s: Optional[float]) -> str:
        if ttc_s is None:
            return "na"
        t = float(ttc_s)
        if t < 60:
            return "<60s"
        if t < 120:
            return "60-120s"
        if t < 240:
            return "120-240s"
        return ">=240s"

    def record_settled(self, *, ttc_s: Optional[float], p_up: Optional[float], won: bool,
                       pnl: float, ev_after_cost: Optional[float] = None,
                       entry_mode: Optional[str] = None) -> None:
        conv = conviction(p_up)
        late = (ttc_s is not None and float(ttc_s) <= self.max_ttc_s)
        high_conv = (conv is not None and conv >= self.min_conviction)
        cohort = "late_high_conviction" if (late and high_conv) else "other"
        ev = float(ev_after_cost or 0.0)
        self._bump(self.cohorts, cohort, won, pnl, ev)
        self._bump(self.by_conviction, conviction_bucket(p_up), won, pnl, ev)
        self._bump(self.by_ttc, self._ttc_bucket(ttc_s), won, pnl, ev)
        self._bump(self.by_entry_mode, entry_mode or "standard", won, pnl, ev)

    @staticmethod
    def _view(s: dict) -> dict:
        n = s["n"]
        return {"n": n, "win_rate": (round(s["wins"] / n, 4) if n else None),
                "pnl_usd": round(s["pnl"], 4),
                "avg_pnl_usd": (round(s["pnl"] / n, 4) if n else None),
                "avg_ev_after_cost": (round(s["ev"] / n, 6) if n else None)}

    def report(self) -> dict:
        lhc = self.cohorts.get("late_high_conviction") or self._stat()
        oth = self.cohorts.get("other") or self._stat()
        lhc_v, oth_v = self._view(lhc), self._view(oth)
        verdict = "insufficient_evidence"
        if lhc["n"] >= self.MIN_COHORT and oth["n"] >= self.MIN_COHORT:
            dwr = (lhc_v["win_rate"] or 0.0) - (oth_v["win_rate"] or 0.0)
            if dwr >= 0.05 and (lhc_v["pnl_usd"] or 0.0) > 0:
                verdict = "time_decay_edge_present"
            elif dwr <= -0.05:
                verdict = "late_window_worse"
            else:
                verdict = "no_clear_edge"
        return {
            "observe_only": True, "report_only": True, "affects_trading": False,
            "reference_max_ttc_s": self.max_ttc_s, "reference_min_conviction": self.min_conviction,
            "min_cohort": self.MIN_COHORT, "verdict": verdict,
            "cohort_late_high_conviction": lhc_v, "cohort_other": oth_v,
            "by_conviction_bucket": {k: self._view(v) for k, v in self.by_conviction.items()},
            "by_ttc_bucket": {k: self._view(v) for k, v in self.by_ttc.items()},
            "by_entry_mode": {k: self._view(v) for k, v in self.by_entry_mode.items()},
            "note": ("observe-only: grades whether late-window high-conviction trades win more "
                     "(cohort vs other) from live settled trades; never affects trading."),
        }

    def to_state(self) -> dict:
        return {"cohorts": {k: dict(v) for k, v in self.cohorts.items()},
                "by_conviction": {k: dict(v) for k, v in self.by_conviction.items()},
                "by_ttc": {k: dict(v) for k, v in self.by_ttc.items()},
                "by_entry_mode": {k: dict(v) for k, v in self.by_entry_mode.items()}}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        def _load(tbl):
            out = {}
            for k, v in (tbl or {}).items():
                s = self._stat()
                s["n"] = int(v.get("n", 0) or 0)
                s["wins"] = int(v.get("wins", 0) or 0)
                s["pnl"] = float(v.get("pnl", 0.0) or 0.0)
                s["ev"] = float(v.get("ev", 0.0) or 0.0)
                out[str(k)] = s
            return out
        self.cohorts = _load(data.get("cohorts"))
        self.by_conviction = _load(data.get("by_conviction"))
        self.by_ttc = _load(data.get("by_ttc"))
        self.by_entry_mode = _load(data.get("by_entry_mode"))
