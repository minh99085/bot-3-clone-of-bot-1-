"""Learned 1-hour entry-timing gate (PAPER ONLY).

1h directional trades were entering within seconds of the hour open (TTC ~3600s) while only the
15m+30m TV ladder was fresh — the 45m/55m bars had not yet formed. This gate treats
*seconds-since-open* as a learned feature: it records settled outcomes per coarse intra-hour bucket,
rejects buckets that are statistically proven losing once enough samples exist, and explores cold or
marginal buckets at a capped rate so the bot keeps learning. A hard ``min_seconds_since_open`` floor
applies to every 1h entry path (including Grok-follow), so immediate open snipes are blocked until
the ladder has time to inform the decision.

Can only make the bot MORE selective — never creates, forces, or fast-tracks a trade.
"""

from __future__ import annotations

import random
from typing import Optional

from engine.pulse.selectivity import (
    SelectivityEvidence,
    benjamini_hochberg,
    breakeven_win_rate,
    profit_factor_from_stat,
    _binom_cdf_le,
    _wilson_upper,
)

HOURLY_WINDOW_SECONDS = 3600

# Coarse intra-hour buckets keyed by seconds since window open.
HOURLY_ENTRY_BUCKETS = (
    ("h0_5m", 0, 300),
    ("h5_15m", 300, 900),
    ("h15_30m", 900, 1800),
    ("h30_45m", 1800, 2700),
    ("h45_60m", 2700, 3600),
)


def hourly_entry_bucket(seconds_since_open: Optional[float],
                        *, window_seconds: int = HOURLY_WINDOW_SECONDS) -> str:
    """Map seconds-since-open to a learnable intra-hour bucket (1h windows only)."""
    if seconds_since_open is None or window_seconds < HOURLY_WINDOW_SECONDS:
        return "na"
    sso = max(0.0, float(seconds_since_open))
    for name, lo, hi in HOURLY_ENTRY_BUCKETS:
        if lo <= sso < hi:
            return name
    if sso >= HOURLY_ENTRY_BUCKETS[-1][1]:
        return HOURLY_ENTRY_BUCKETS[-1][0]
    return "na"


def is_hourly_window(window_seconds: Optional[int]) -> bool:
    return int(window_seconds or 0) >= HOURLY_WINDOW_SECONDS


def hourly_lane_bucket(bucket: str, *, asset: Optional[str] = None,
                       side: Optional[str] = None) -> str:
    """Keep BTC/ETH and UP/DOWN timing evidence independent."""
    a = str(asset or "").lower()
    s = str(side or "").lower()
    return f"{a}|{s}|{bucket}" if a in ("btc", "eth") and s in ("up", "down") else str(bucket)


class HourlyEntryEvidence:
    """Per-bucket settled-trade evidence for 1h entry timing."""

    def __init__(self):
        self.buckets: dict = {}

    @staticmethod
    def _stat() -> dict:
        return {"n": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0, "ev": 0.0}

    def record(self, bucket: str, *, won: bool, pnl: float,
               ev_after_cost: Optional[float] = None) -> None:
        b = str(bucket or "na")
        if b == "na":
            return
        s = self.buckets.setdefault(b, self._stat())
        s["n"] += 1
        s["wins"] += int(bool(won))
        pnl = float(pnl or 0.0)
        s["pnl"] = round(s["pnl"] + pnl, 6)
        if pnl > 0:
            s["gross_win"] = round(s["gross_win"] + pnl, 6)
        elif pnl < 0:
            s["gross_loss"] = round(s["gross_loss"] + (-pnl), 6)
        s["ev"] = round(s["ev"] + float(ev_after_cost or 0.0), 6)

    def stat(self, bucket: str) -> Optional[dict]:
        s = self.buckets.get(str(bucket))
        if not s or s["n"] <= 0:
            return None
        n = s["n"]
        losses = n - s["wins"]
        return {
            "n": n,
            "win_rate": round(s["wins"] / n, 4),
            "pnl_usd": round(s["pnl"], 4),
            "avg_win": round(s["gross_win"] / s["wins"], 6) if s["wins"] else 0.0,
            "avg_loss": round(s["gross_loss"] / losses, 6) if losses else 0.0,
            "avg_ev_after_cost": round(s["ev"] / n, 6),
        }

    def to_state(self) -> dict:
        return {"buckets": {b: dict(s) for b, s in self.buckets.items()}}

    def load_state(self, data: dict) -> None:
        self.buckets = {}
        for b, s in (data or {}).get("buckets", {}).items():
            st = self._stat()
            for k in st:
                st[k] = (int(s.get(k, 0)) if k in ("n", "wins")
                         else float(s.get(k, 0.0) or 0.0))
            self.buckets[str(b)] = st

    @property
    def has_data(self) -> bool:
        return bool(self.buckets)


class LearnedHourlyEntryGate:
    """Reject proven-losing intra-hour entry buckets on 1h windows; explore cold buckets."""

    def __init__(self, *, enabled: bool = True, min_seconds_since_open: float = 180.0,
                 max_seconds_since_open: Optional[float] = 2700.0,
                 min_samples: int = 20, min_profit_factor: float = 0.85,
                 exploration_rate: float = 0.08, confidence_z: float = 1.64,
                 fdr_q: float = 0.10, seed: Optional[int] = None):
        self.enabled = bool(enabled)
        self.min_seconds_since_open = max(0.0, float(min_seconds_since_open))
        self.max_seconds_since_open = (
            None if max_seconds_since_open is None
            else max(0.0, float(max_seconds_since_open)))
        self.min_samples = int(min_samples)
        self.min_profit_factor = float(min_profit_factor)
        self.fdr_q = float(fdr_q)
        self.confidence_z = float(confidence_z)
        self.exploration_rate = max(0.0, min(0.10, float(exploration_rate)))
        self.accepted = 0
        self.rejected = 0
        self.explored = 0
        self.too_early = 0
        self.too_late = 0
        self.reject_reasons: dict = {}
        self.by_decision: dict = {}
        self._rng = random.Random(seed)

    def _assess(self, st: dict) -> dict:
        n = int(st["n"])
        wr = float(st["win_rate"])
        wins = int(round(wr * n))
        be = breakeven_win_rate(st["avg_win"], st["avg_loss"])
        upper = _wilson_upper(wins, n, self.confidence_z)
        ev_per_trade = round(wr * float(st["avg_win"]) - (1.0 - wr) * float(st["avg_loss"]), 4)
        pf = profit_factor_from_stat(st)
        pf_ok = (pf is not None and pf < self.min_profit_factor)
        confidently_losing = (st["pnl_usd"] < 0) and (upper < be) and pf_ok
        p_below_breakeven = _binom_cdf_le(wins, n, be) if n > 0 else 1.0
        return {
            "n": n, "win_rate": round(wr, 4), "pnl_usd": st["pnl_usd"],
            "avg_win": st["avg_win"], "avg_loss": st["avg_loss"],
            "profit_factor": pf, "breakeven_win_rate": round(be, 4),
            "win_rate_upper_ci": round(upper, 4),
            "ev_per_trade": ev_per_trade,
            "p_value_vs_breakeven": round(p_below_breakeven, 6),
            "confidently_losing": confidently_losing,
        }

    def _eligible_block_buckets(self, evidence: HourlyEntryEvidence) -> dict:
        rows, keys = [], []
        for b in evidence.buckets:
            st = evidence.stat(b)
            if not st or st["n"] < self.min_samples:
                continue
            a = self._assess(st)
            if not a.get("confidently_losing"):
                continue
            rows.append(a)
            keys.append(str(b))
        if not rows:
            return {}
        flags = benjamini_hochberg([r["p_value_vs_breakeven"] for r in rows], q=self.fdr_q)
        out = {}
        for b, a, ok in zip(keys, rows, flags):
            a = dict(a)
            a["fdr_significant"] = bool(ok)
            a["block_allowed"] = bool(ok)
            out[b] = {"bucket": b, **a}
        return out

    def evaluate(self, *, window_seconds: int, seconds_since_open: float,
                 asset: Optional[str] = None, side: Optional[str] = None,
                 evidence: HourlyEntryEvidence) -> dict:
        """Return {decision, reasons, bucket, bad_bucket, seconds_since_open}."""
        if not self.enabled or not is_hourly_window(window_seconds):
            self.accepted += 1
            return {"decision": "accept", "reasons": ["gate_disabled_or_not_hourly"],
                    "bucket": "na", "bad_bucket": None,
                    "seconds_since_open": round(float(seconds_since_open), 1)}
        sso = float(seconds_since_open)
        bucket = hourly_entry_bucket(sso, window_seconds=window_seconds)
        evidence_bucket = hourly_lane_bucket(bucket, asset=asset, side=side)
        if sso < self.min_seconds_since_open:
            self.too_early += 1
            self.rejected += 1
            reason = "hourly_too_early"
            self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
            return {
                "decision": "reject",
                "reasons": [reason],
                "bucket": bucket,
                "bad_bucket": None,
                "seconds_since_open": round(sso, 1),
                "min_seconds_since_open": self.min_seconds_since_open,
            }
        if (self.max_seconds_since_open is not None
                and sso > self.max_seconds_since_open):
            self.too_late += 1
            self.rejected += 1
            reason = "hourly_too_late"
            self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
            return {
                "decision": "reject",
                "reasons": [reason],
                "bucket": bucket,
                "bad_bucket": None,
                "seconds_since_open": round(sso, 1),
                "max_seconds_since_open": self.max_seconds_since_open,
            }
        allowed = self._eligible_block_buckets(evidence)
        bad = allowed.get(evidence_bucket)
        if not bad:
            self.accepted += 1
            return {"decision": "accept", "reasons": [], "bucket": bucket,
                    "bad_bucket": None, "seconds_since_open": round(sso, 1)}
        reason = f"bad_hourly_bucket:{evidence_bucket}"
        if self.exploration_rate > 0 and self._rng.random() < self.exploration_rate:
            self.explored += 1
            return {"decision": "explore", "reasons": [reason], "bucket": bucket,
                    "bad_bucket": bad, "seconds_since_open": round(sso, 1),
                    "exploration": True}
        self.rejected += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
        return {"decision": "reject", "reasons": [reason], "bucket": bucket,
                "bad_bucket": bad, "seconds_since_open": round(sso, 1)}

    def record_settled(self, gate_decision: Optional[str], *, won: bool, pnl: float) -> None:
        s = self.by_decision.setdefault(str(gate_decision or "passed"),
                                        {"n": 0, "wins": 0, "pnl": 0.0})
        s["n"] += 1
        s["wins"] += int(bool(won))
        s["pnl"] = round(s["pnl"] + float(pnl or 0.0), 6)

    def bucket_evidence(self, evidence: HourlyEntryEvidence, *, top: int = 8) -> dict:
        allowed = self._eligible_block_buckets(evidence)
        rows = []
        for b in evidence.buckets:
            st = evidence.stat(b)
            if not st:
                continue
            a = self._assess(st)
            hit = allowed.get(str(b))
            a["block_allowed"] = bool(hit and hit.get("block_allowed"))
            a["fdr_significant"] = bool(hit and hit.get("fdr_significant"))
            rows.append({"bucket": str(b), **a})
        rows.sort(key=lambda r: (not r.get("block_allowed"), r.get("ev_per_trade", 0.0)))
        return {"min_samples": self.min_samples, "buckets": rows[:top]}

    def report(self, *, evidence: Optional[HourlyEntryEvidence] = None) -> dict:
        pnl_by = {k: {"n": v["n"],
                      "win_rate": (round(v["wins"] / v["n"], 4) if v["n"] else None),
                      "pnl_usd": round(v["pnl"], 4)}
                  for k, v in self.by_decision.items()}
        out = {
            "enabled": self.enabled,
            "min_seconds_since_open": self.min_seconds_since_open,
            "max_seconds_since_open": self.max_seconds_since_open,
            "target_entry_band_s": [self.min_seconds_since_open, self.max_seconds_since_open],
            "min_samples": self.min_samples,
            "exploration_rate": self.exploration_rate,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "explored": self.explored,
            "too_early": self.too_early,
            "too_late": self.too_late,
            "reject_reasons": dict(self.reject_reasons),
            "pnl_by_gate_decision": pnl_by,
            "note": ("1h-only: rejects entries before min_seconds_since_open or after "
                     "max_seconds_since_open, plus proven-losing intra-hour buckets."),
        }
        if evidence is not None:
            out["bucket_evidence"] = self.bucket_evidence(evidence)
        return out

    def to_state(self) -> dict:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "explored": self.explored,
            "too_early": self.too_early,
            "too_late": self.too_late,
            "reject_reasons": dict(self.reject_reasons),
            "by_decision": {k: dict(v) for k, v in self.by_decision.items()},
        }

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.accepted = int(data.get("accepted", 0) or 0)
        self.rejected = int(data.get("rejected", 0) or 0)
        self.explored = int(data.get("explored", 0) or 0)
        self.too_early = int(data.get("too_early", 0) or 0)
        self.too_late = int(data.get("too_late", 0) or 0)
        self.reject_reasons = {k: int(v or 0) for k, v in (data.get("reject_reasons") or {}).items()}
        self.by_decision = {
            k: {"n": int(v.get("n", 0) or 0), "wins": int(v.get("wins", 0) or 0),
                "pnl": float(v.get("pnl", 0.0) or 0.0)}
            for k, v in (data.get("by_decision") or {}).items()
        }

