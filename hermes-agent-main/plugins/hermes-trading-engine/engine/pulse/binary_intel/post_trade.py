"""Post-trade binary intelligence — advance loop learning from settled outcomes.

After every official Polymarket resolution:
  1. Grade pre-trade intelligence_score vs win (Brier-style)
  2. Grade 5m RSI overlay alignment (fixes prior learning gap)
  3. Self-tune component weights for next pre-trade blend
  4. Emit / refresh LessonsBook rules for Grok
  5. Produce Grok autopsy brief for deep compute

PAPER ONLY. Never mutates execution_gate authority.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional

def _wilson_lower(wins: int, n: int, z: float = 1.64) -> float:
    """One-sided Wilson lower bound on win rate."""
    if n <= 0:
        return 0.0
    import math
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    spread = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return max(0.0, (centre - spread) / denom)


def _bucket(score: Optional[float]) -> str:
    if score is None:
        return "unknown"
    s = float(score)
    if s >= 0.70:
        return "high"
    if s >= 0.55:
        return "mid_high"
    if s >= 0.40:
        return "mid"
    if s >= 0.28:
        return "low"
    return "critical"


class BinaryIntelLearner:
    """Settled-outcome learner for binary_intel pre-trade scores + RSI overlays."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        lookback_n: int = 80,
        min_samples: int = 12,
        target_wr: float = 0.58,
        kill_wr: float = 0.45,
    ):
        self.enabled = bool(enabled)
        self.lookback_n = int(lookback_n)
        self.min_samples = int(min_samples)
        self.target_wr = float(target_wr)
        self.kill_wr = float(kill_wr)
        self._recent: Deque[dict] = deque(maxlen=max(20, self.lookback_n))
        self._brier_sum = 0.0
        self._brier_n = 0
        self._adjustments: list = []
        self._weights = {
            "intel": 0.55,
            "readiness": 0.30,
            "tv_confirm": 0.15,
        }
        self._last_action: Optional[str] = None
        self._last_ts: Optional[float] = None

    def record_settled(
        self,
        *,
        won: bool,
        pnl_usd: float,
        side: Optional[str] = None,
        asset: str = "btc",
        lane: str = "15m",
        intel_score: Optional[float] = None,
        composite_score: Optional[float] = None,
        rsi_lean: Optional[str] = None,
        rsi_aligned: Optional[bool] = None,
        rsi_decision: Optional[str] = None,
        displacement_z: Optional[float] = None,
        now: Optional[float] = None,
    ) -> Optional[dict]:
        if not self.enabled:
            return None
        ts = float(now if now is not None else time.time())
        # Brier for composite as P(win) proxy
        p = float(composite_score) if composite_score is not None else (
            float(intel_score) if intel_score is not None else 0.5)
        p = max(0.0, min(1.0, p))
        y = 1.0 if won else 0.0
        self._brier_sum += (p - y) ** 2
        self._brier_n += 1

        row = {
            "ts": ts,
            "won": bool(won),
            "pnl": float(pnl_usd or 0.0),
            "side": str(side or "").lower() or None,
            "asset": str(asset or "btc").lower(),
            "lane": str(lane or "15m"),
            "intel_score": intel_score,
            "composite_score": composite_score,
            "intel_bucket": _bucket(composite_score if composite_score is not None else intel_score),
            "rsi_lean": (str(rsi_lean).lower() if rsi_lean else None),
            "rsi_aligned": rsi_aligned,
            "rsi_decision": rsi_decision,
            "displacement_z": displacement_z,
        }
        self._recent.append(row)
        return row

    def _wr(self, rows: list) -> tuple:
        n = len(rows)
        if n <= 0:
            return 0.0, 0, 0.0
        wins = sum(1 for r in rows if r.get("won"))
        return wins / n, n, _wilson_lower(wins, n)

    def maybe_adjust(self, *, now: Optional[float] = None) -> Optional[dict]:
        if not self.enabled or len(self._recent) < self.min_samples:
            return None
        rows = list(self._recent)
        wr, n, wlb = self._wr(rows)
        action = None
        detail = {}

        # High-score buckets should beat low-score — if inverted, rebalance weights toward TV
        high = [r for r in rows if r.get("intel_bucket") in ("high", "mid_high")]
        low = [r for r in rows if r.get("intel_bucket") in ("low", "critical")]
        if len(high) >= 5 and len(low) >= 5:
            wr_h, _, _ = self._wr(high)
            wr_l, _, _ = self._wr(low)
            detail["wr_high"] = round(wr_h, 4)
            detail["wr_low"] = round(wr_l, 4)
            if wr_h + 0.05 < wr_l:
                # Score inverted — boost TV confirm weight
                self._weights["tv_confirm"] = min(0.30, self._weights["tv_confirm"] + 0.03)
                self._weights["intel"] = max(0.40, self._weights["intel"] - 0.02)
                self._weights["readiness"] = 1.0 - self._weights["intel"] - self._weights["tv_confirm"]
                action = "reweight_tv"
            elif wr_h > wr_l + 0.08 and wr_h >= self.target_wr:
                self._weights["intel"] = min(0.65, self._weights["intel"] + 0.02)
                self._weights["tv_confirm"] = max(0.10, self._weights["tv_confirm"] - 0.01)
                self._weights["readiness"] = 1.0 - self._weights["intel"] - self._weights["tv_confirm"]
                action = "reweight_intel"

        # RSI alignment grading
        aligned = [r for r in rows if r.get("rsi_aligned") is True]
        opposed = [r for r in rows if r.get("rsi_aligned") is False]
        if len(aligned) >= 6 and len(opposed) >= 4:
            wr_a, _, _ = self._wr(aligned)
            wr_o, _, _ = self._wr(opposed)
            detail["wr_rsi_aligned"] = round(wr_a, 4)
            detail["wr_rsi_opposed"] = round(wr_o, 4)
            if wr_a < wr_o and action is None:
                action = "rsi_signal_weak"
            elif wr_a >= self.target_wr and wr_a > wr_o + 0.05 and action is None:
                action = "rsi_confirm_strong"

        if wr < self.kill_wr and n >= self.min_samples and action is None:
            action = "overall_kill_tighten"

        if action is None:
            return None

        ts = float(now if now is not None else time.time())
        adj = {
            "ts": ts,
            "action": action,
            "wr": round(wr, 4),
            "wilson_lb": round(wlb, 4),
            "n": n,
            "weights": dict(self._weights),
            "detail": detail,
        }
        self._adjustments.append(adj)
        self._adjustments = self._adjustments[-40:]
        self._last_action = action
        self._last_ts = ts
        return adj

    def lessons_for_book(self) -> list:
        """Return (kind, key, rule) tuples for LessonsBook."""
        rows = list(self._recent)
        out = []
        if len(rows) < self.min_samples:
            return out

        aligned = [r for r in rows if r.get("rsi_aligned") is True]
        opposed = [r for r in rows if r.get("rsi_aligned") is False]
        if len(aligned) >= 6 and len(opposed) >= 4:
            wr_a, na, _ = self._wr(aligned)
            wr_o, no, _ = self._wr(opposed)
            if wr_a >= self.target_wr and wr_a > wr_o + 0.05:
                out.append((
                    "exploit",
                    "binary_intel:rsi_aligned",
                    "5m RSI divergence ALIGNED with side → WR %.0f%% (n=%d); prefer confirm size."
                    % (100 * wr_a, na),
                ))
            if wr_o < self.kill_wr:
                out.append((
                    "avoid",
                    "binary_intel:rsi_opposed",
                    "5m RSI divergence OPPOSED to side → WR %.0f%% (n=%d); fade / size down."
                    % (100 * wr_o, no),
                ))

        high = [r for r in rows if r.get("intel_bucket") in ("high", "mid_high")]
        critical = [r for r in rows if r.get("intel_bucket") == "critical"]
        if len(critical) >= 5:
            wr_c, nc, _ = self._wr(critical)
            if wr_c < self.kill_wr:
                out.append((
                    "avoid",
                    "binary_intel:critical_score",
                    "binary_intel composite critical → WR %.0f%% (n=%d); wait/explore only."
                    % (100 * wr_c, nc),
                ))
        if len(high) >= 8:
            wr_h, nh, _ = self._wr(high)
            if wr_h >= self.target_wr:
                out.append((
                    "exploit",
                    "binary_intel:high_score",
                    "binary_intel high/mid_high → WR %.0f%% (n=%d); trust math+TV composite."
                    % (100 * wr_h, nh),
                ))
        return out

    def grok_autopsy_brief(self, row: dict, *, won: bool) -> dict:
        return {
            "role": "post_trade_binary_intel",
            "task": (
                "Autopsy this settled Polymarket binary. Compare pre-trade intelligence_score, "
                "displacement_z, RSI lean, and outcome. Extract one durable lesson for next "
                "pre-trade. Be quantitative; cite WR buckets when known."
            ),
            "outcome_won": bool(won),
            "settled": row,
            "learner": {
                "brier": (round(self._brier_sum / self._brier_n, 6)
                          if self._brier_n else None),
                "n": len(self._recent),
                "weights": dict(self._weights),
                "last_action": self._last_action,
            },
        }

    def report(self) -> dict:
        rows = list(self._recent)
        wr, n, wlb = self._wr(rows) if rows else (0.0, 0, 0.0)
        return {
            "enabled": self.enabled,
            "n": n,
            "wr": round(wr, 4) if n else None,
            "wilson_lb": round(wlb, 4) if n else None,
            "brier": (round(self._brier_sum / self._brier_n, 6) if self._brier_n else None),
            "weights": dict(self._weights),
            "last_action": self._last_action,
            "last_ts": self._last_ts,
            "recent_adjustments": list(self._adjustments[-5:]),
            "rsi_aligned_n": sum(1 for r in rows if r.get("rsi_aligned") is True),
            "rsi_opposed_n": sum(1 for r in rows if r.get("rsi_aligned") is False),
        }

    def to_state(self) -> dict:
        return {
            "enabled": self.enabled,
            "recent": list(self._recent),
            "brier_sum": self._brier_sum,
            "brier_n": self._brier_n,
            "weights": dict(self._weights),
            "adjustments": list(self._adjustments[-40:]),
            "last_action": self._last_action,
            "last_ts": self._last_ts,
        }

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self._recent = deque(list(data.get("recent") or []), maxlen=max(20, self.lookback_n))
        self._brier_sum = float(data.get("brier_sum") or 0.0)
        self._brier_n = int(data.get("brier_n") or 0)
        w = data.get("weights") or {}
        for k in self._weights:
            if k in w:
                self._weights[k] = float(w[k])
        self._adjustments = list(data.get("adjustments") or [])[-40:]
        self._last_action = data.get("last_action")
        self._last_ts = data.get("last_ts")
