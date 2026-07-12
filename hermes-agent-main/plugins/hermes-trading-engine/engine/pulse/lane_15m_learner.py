"""15m directional lane strategy learner (PAPER ONLY).

Learns from settled 15m BTC/ETH fills and mutates *lane-local* strategy knobs to raise WR:

  * side preference (both / down_bias / up_bias / down_only / up_only)
  * entry timing band (SSO / TTC sweet)
  * price sweet band
  * min edge / min entry floors
  * probe vs harvest aggressiveness

Does NOT touch Loop Engineering lanes / maker-checker / coordinator.
Does NOT mutate the shared hourly GateAutoTuner targets — keeps a separate policy
so hourly and 15m can diverge.

Evidence rules (Wilson-aware, restrict-first):
  * WR below kill → tighten (narrow sweet, raise edge, bias to winning side)
  * WR above target + enough fills → mild tighten (lock edge)
  * Starved fills → loosen toward exploration floors
  * Per-side / per-TTC / per-price buckets drive side preference + timing shifts
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _wilson_lb(wins: int, n: int, z: float = 1.64) -> float:
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


@dataclass
class Lane15mPolicy:
    """Mutable strategy knobs for the 15m directional lane only."""

    # Side strategy
    side_mode: str = "both"  # both | down_bias | up_bias | down_only | up_only
    # Timing (seconds) — light defaults; learner tightens from evidence
    min_sso: float = 60.0
    max_sso: float = 780.0
    prefer_ttc_min: float = 120.0
    prefer_ttc_max: float = 420.0
    # Price / edge
    sweet_min: float = 0.45
    sweet_max: float = 0.85
    min_edge: float = 0.02
    min_entry_price: float = 0.40
    # Aggressiveness
    probe_enabled: bool = True
    harvest_edge_min: float = 0.02
    strike_edge_min: float = 0.03
    # Caps (paper)
    max_size_usd: float = 25.0


@dataclass
class Lane15mLearnerConfig:
    enabled: bool = True
    lookback_n: int = 48
    min_samples: int = 10
    target_wr: float = 0.60
    kill_wr: float = 0.45
    starve_fills_per_hour: float = 1.5   # 15m has more windows → higher starve floor
    rich_fills_per_hour: float = 8.0
    cooldown_settlements: int = 4
    side_min_n: int = 6


class Lane15mStrategyLearner:
    """Settled-outcome learner that rewrites Lane15mPolicy to chase higher WR."""

    def __init__(
        self,
        cfg: Optional[Lane15mLearnerConfig] = None,
        policy: Optional[Lane15mPolicy] = None,
    ):
        self.cfg = cfg or Lane15mLearnerConfig()
        self.policy = policy or Lane15mPolicy()
        self._recent: Deque[dict] = deque(maxlen=max(16, int(self.cfg.lookback_n)))
        self._since_adjust = 0
        self._adjustments: list = []
        self._last_action: Optional[str] = None
        self._last_ts: Optional[float] = None

    # ---- evidence ----
    def record_settled(
        self,
        *,
        won: bool,
        pnl_usd: float,
        side: Optional[str] = None,
        entry_price: Optional[float] = None,
        asset: str = "btc",
        sso: Optional[float] = None,
        ttc_s: Optional[float] = None,
        entry_mode: Optional[str] = None,
        chart_lean_aligned: Optional[bool] = None,
        chart_alignment: Optional[str] = None,
        short_pattern: Optional[str] = None,
        rsi_overlay_aligned: Optional[bool] = None,
        now: Optional[float] = None,
    ) -> None:
        if not self.cfg.enabled:
            return
        ts = float(now if now is not None else time.time())
        # Prefer RSI overlay alignment when present (Bot 3: 5m RSI div is the active TV signal).
        lean_aligned = chart_lean_aligned
        if rsi_overlay_aligned is not None:
            lean_aligned = bool(rsi_overlay_aligned)
        self._recent.append({
            "won": bool(won),
            "pnl": float(pnl_usd or 0.0),
            "side": str(side or "").lower() or None,
            "entry_price": (float(entry_price) if entry_price is not None else None),
            "asset": str(asset or "btc").lower(),
            "sso": float(sso) if sso is not None else None,
            "ttc_s": float(ttc_s) if ttc_s is not None else None,
            "entry_mode": str(entry_mode or "") or None,
            "chart_lean_aligned": (bool(lean_aligned) if lean_aligned is not None else None),
            "chart_alignment": str(chart_alignment or "") or None,
            "short_pattern": str(short_pattern or "") or None,
            "rsi_overlay_aligned": (bool(rsi_overlay_aligned)
                                    if rsi_overlay_aligned is not None else None),
            "settled_ts": ts,
        })
        self._since_adjust += 1

    def _rolling(self) -> dict:
        rows = list(self._recent)
        n = len(rows)
        if n == 0:
            return {"n": 0, "wins": 0, "win_rate": None, "pnl_usd": 0.0,
                    "fills_per_hour": 0.0, "by_side": {}, "by_asset": {},
                    "by_ttc": {}, "by_price": {}}
        wins = sum(1 for r in rows if r["won"])
        pnl = sum(float(r["pnl"]) for r in rows)
        t0 = min(float(r["settled_ts"]) for r in rows)
        t1 = max(float(r["settled_ts"]) for r in rows)
        hours = max(1.0 / 60.0, (t1 - t0) / 3600.0) if n >= 2 else 1.0

        def _bucket(key_fn):
            out: dict = {}
            for r in rows:
                k = key_fn(r)
                if k is None:
                    continue
                st = out.setdefault(k, {"n": 0, "wins": 0, "pnl_usd": 0.0})
                st["n"] += 1
                if r["won"]:
                    st["wins"] += 1
                st["pnl_usd"] = round(st["pnl_usd"] + float(r["pnl"]), 4)
            for st in out.values():
                st["win_rate"] = round(st["wins"] / st["n"], 4) if st["n"] else None
                st["wilson_lb"] = round(_wilson_lb(st["wins"], st["n"]), 4)
            return out

        def ttc_band(r):
            t = r.get("ttc_s")
            if t is None:
                return None
            if t < 120:
                return "ttc_0_120"
            if t < 240:
                return "ttc_120_240"
            if t < 420:
                return "ttc_240_420"
            return "ttc_420_plus"

        def price_band(r):
            p = r.get("entry_price")
            if p is None:
                return None
            if p < 0.45:
                return "p_lt_45"
            if p < 0.55:
                return "p_45_55"
            if p < 0.70:
                return "p_55_70"
            return "p_70_plus"

        return {
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4),
            "pnl_usd": round(pnl, 4),
            "fills_per_hour": round(n / hours, 4),
            "span_hours": round(hours, 3),
            "by_side": _bucket(lambda r: r.get("side")),
            "by_asset": _bucket(lambda r: r.get("asset")),
            "by_ttc": _bucket(ttc_band),
            "by_price": _bucket(price_band),
            "by_chart_lean": _bucket(
                lambda r: ("lean_aligned" if r.get("chart_lean_aligned") is True
                           else ("lean_opposed" if r.get("chart_lean_aligned") is False
                                 else None))),
            "by_chart_alignment": _bucket(lambda r: r.get("chart_alignment")),
            "by_short_pattern": _bucket(lambda r: r.get("short_pattern")),
        }

    def _pick_side_mode(self, roll: dict) -> str:
        by = roll.get("by_side") or {}
        up = by.get("up") or {"n": 0, "wins": 0, "wilson_lb": 0.0, "win_rate": None}
        dn = by.get("down") or {"n": 0, "wins": 0, "wilson_lb": 0.0, "win_rate": None}
        min_n = int(self.cfg.side_min_n)
        up_n, dn_n = int(up.get("n") or 0), int(dn.get("n") or 0)
        if up_n < min_n and dn_n < min_n:
            return self.policy.side_mode  # keep exploring
        up_lb = float(up.get("wilson_lb") or 0.0)
        dn_lb = float(dn.get("wilson_lb") or 0.0)
        kill = float(self.cfg.kill_wr)
        target = float(self.cfg.target_wr)
        # One side proven bad → bias / lock to the other
        if up_n >= min_n and up_lb < kill and dn_n >= min_n and dn_lb >= target:
            return "down_only"
        if dn_n >= min_n and dn_lb < kill and up_n >= min_n and up_lb >= target:
            return "up_only"
        if up_n >= min_n and up_lb < kill and dn_n < min_n:
            return "down_bias"
        if dn_n >= min_n and dn_lb < kill and up_n < min_n:
            return "up_bias"
        if dn_n >= min_n and up_n >= min_n:
            if dn_lb > up_lb + 0.08:
                return "down_bias"
            if up_lb > dn_lb + 0.08:
                return "up_bias"
        return "both"

    def _pick_timing(self, roll: dict) -> tuple:
        by = roll.get("by_ttc") or {}
        if not by:
            return self.policy.prefer_ttc_min, self.policy.prefer_ttc_max
        # Prefer the band with best Wilson LB among those with enough samples
        best = None
        best_lb = -1.0
        for name, st in by.items():
            if int(st.get("n") or 0) < 4:
                continue
            lb = float(st.get("wilson_lb") or 0.0)
            if lb > best_lb:
                best_lb = lb
                best = name
        mapping = {
            "ttc_0_120": (30.0, 120.0),
            "ttc_120_240": (120.0, 240.0),
            "ttc_240_420": (240.0, 420.0),
            "ttc_420_plus": (420.0, 720.0),
        }
        if best and best in mapping:
            return mapping[best]
        return self.policy.prefer_ttc_min, self.policy.prefer_ttc_max

    def _pick_sweet(self, roll: dict) -> tuple:
        by = roll.get("by_price") or {}
        if not by:
            return self.policy.sweet_min, self.policy.sweet_max
        best = None
        best_lb = -1.0
        for name, st in by.items():
            if int(st.get("n") or 0) < 4:
                continue
            lb = float(st.get("wilson_lb") or 0.0)
            if lb > best_lb:
                best_lb = lb
                best = name
        mapping = {
            "p_lt_45": (0.35, 0.50),
            "p_45_55": (0.45, 0.58),
            "p_55_70": (0.52, 0.72),
            "p_70_plus": (0.65, 0.85),
        }
        if best and best in mapping:
            return mapping[best]
        return self.policy.sweet_min, self.policy.sweet_max

    def _decide(self, roll: dict) -> Optional[str]:
        n = int(roll.get("n") or 0)
        if n < int(self.cfg.min_samples):
            fph = float(roll.get("fills_per_hour") or 0.0)
            if n >= 4 and fph < float(self.cfg.starve_fills_per_hour) * 0.4:
                return "loosen"
            return None
        wr = roll.get("win_rate")
        if wr is None:
            return None
        wr = float(wr)
        fph = float(roll.get("fills_per_hour") or 0.0)
        if wr < float(self.cfg.kill_wr):
            return "tighten"
        if fph < float(self.cfg.starve_fills_per_hour):
            return "loosen"
        if wr >= float(self.cfg.target_wr) and fph > float(self.cfg.rich_fills_per_hour):
            return "tighten"
        if wr < float(self.cfg.target_wr) and fph >= float(self.cfg.rich_fills_per_hour):
            return "tighten"
        return "rebalance"  # always allow side/timing rebalance when enough samples

    def maybe_adjust(self) -> Optional[dict]:
        """Rewrite self.policy from rolling evidence. Returns adjustment dict or None."""
        if not self.cfg.enabled:
            return None
        if self._since_adjust < int(self.cfg.cooldown_settlements):
            return None
        roll = self._rolling()
        action = self._decide(roll)
        if action is None:
            return None

        before = {
            "side_mode": self.policy.side_mode,
            "min_sso": self.policy.min_sso,
            "max_sso": self.policy.max_sso,
            "prefer_ttc_min": self.policy.prefer_ttc_min,
            "prefer_ttc_max": self.policy.prefer_ttc_max,
            "sweet_min": self.policy.sweet_min,
            "sweet_max": self.policy.sweet_max,
            "min_edge": self.policy.min_edge,
            "min_entry_price": self.policy.min_entry_price,
            "probe_enabled": self.policy.probe_enabled,
            "harvest_edge_min": self.policy.harvest_edge_min,
        }

        new_side = self._pick_side_mode(roll)
        ttc_lo, ttc_hi = self._pick_timing(roll)
        sweet_lo, sweet_hi = self._pick_sweet(roll)

        if action == "tighten":
            self.policy.min_edge = _clamp(self.policy.min_edge + 0.005, 0.02, 0.10)
            self.policy.min_entry_price = _clamp(
                self.policy.min_entry_price + 0.01, 0.40, 0.62)
            self.policy.harvest_edge_min = _clamp(
                self.policy.harvest_edge_min + 0.005, 0.02, 0.08)
            self.policy.strike_edge_min = _clamp(
                self.policy.strike_edge_min + 0.005, 0.03, 0.10)
            # Narrow toward best bands
            self.policy.sweet_min = _clamp(max(self.policy.sweet_min, sweet_lo), 0.35, 0.70)
            self.policy.sweet_max = _clamp(min(self.policy.sweet_max, sweet_hi), 0.50, 0.90)
            self.policy.prefer_ttc_min = float(ttc_lo)
            self.policy.prefer_ttc_max = float(ttc_hi)
            # Convert SSO from preferred TTC (window=900)
            self.policy.min_sso = _clamp(900.0 - self.policy.prefer_ttc_max, 30.0, 600.0)
            self.policy.max_sso = _clamp(900.0 - self.policy.prefer_ttc_min, 120.0, 870.0)
            if float(roll.get("win_rate") or 0) < float(self.cfg.kill_wr):
                self.policy.probe_enabled = False
            self.policy.side_mode = new_side
        elif action == "loosen":
            self.policy.min_edge = _clamp(self.policy.min_edge - 0.005, 0.015, 0.10)
            self.policy.min_entry_price = _clamp(
                self.policy.min_entry_price - 0.01, 0.35, 0.62)
            self.policy.harvest_edge_min = _clamp(
                self.policy.harvest_edge_min - 0.005, 0.015, 0.08)
            self.policy.sweet_min = _clamp(self.policy.sweet_min - 0.02, 0.35, 0.70)
            self.policy.sweet_max = _clamp(self.policy.sweet_max + 0.02, 0.50, 0.90)
            self.policy.min_sso = _clamp(self.policy.min_sso - 30.0, 30.0, 600.0)
            self.policy.max_sso = _clamp(self.policy.max_sso + 30.0, 120.0, 870.0)
            self.policy.probe_enabled = True
            if self.policy.side_mode in ("down_only", "up_only"):
                self.policy.side_mode = (
                    "down_bias" if self.policy.side_mode == "down_only" else "up_bias")
            else:
                self.policy.side_mode = new_side
        else:  # rebalance
            self.policy.side_mode = new_side
            self.policy.prefer_ttc_min = float(ttc_lo)
            self.policy.prefer_ttc_max = float(ttc_hi)
            self.policy.min_sso = _clamp(900.0 - self.policy.prefer_ttc_max, 30.0, 600.0)
            self.policy.max_sso = _clamp(900.0 - self.policy.prefer_ttc_min, 120.0, 870.0)
            # Mild sweet nudge toward best price band without hard lock
            self.policy.sweet_min = _clamp(
                0.7 * self.policy.sweet_min + 0.3 * sweet_lo, 0.35, 0.70)
            self.policy.sweet_max = _clamp(
                0.7 * self.policy.sweet_max + 0.3 * sweet_hi, 0.50, 0.90)

        if self.policy.sweet_max < self.policy.sweet_min + 0.08:
            self.policy.sweet_max = self.policy.sweet_min + 0.08

        after = {
            "side_mode": self.policy.side_mode,
            "min_sso": round(self.policy.min_sso, 1),
            "max_sso": round(self.policy.max_sso, 1),
            "prefer_ttc_min": round(self.policy.prefer_ttc_min, 1),
            "prefer_ttc_max": round(self.policy.prefer_ttc_max, 1),
            "sweet_min": round(self.policy.sweet_min, 4),
            "sweet_max": round(self.policy.sweet_max, 4),
            "min_edge": round(self.policy.min_edge, 4),
            "min_entry_price": round(self.policy.min_entry_price, 4),
            "probe_enabled": self.policy.probe_enabled,
            "harvest_edge_min": round(self.policy.harvest_edge_min, 4),
        }
        adj = {
            "ts": time.time(),
            "action": action,
            "reason": {
                "win_rate": roll.get("win_rate"),
                "fills_per_hour": roll.get("fills_per_hour"),
                "n": roll.get("n"),
                "pnl_usd": roll.get("pnl_usd"),
                "by_side": roll.get("by_side"),
                "by_ttc": roll.get("by_ttc"),
                "by_price": roll.get("by_price"),
                "target_wr": self.cfg.target_wr,
                "kill_wr": self.cfg.kill_wr,
            },
            "before": before,
            "after": after,
        }
        self._adjustments.append(adj)
        if len(self._adjustments) > 40:
            self._adjustments = self._adjustments[-40:]
        self._since_adjust = 0
        self._last_action = action
        self._last_ts = adj["ts"]
        return adj

    # ---- apply policy to a candidate (used by engine tick) ----
    def filter_side(self, side: Optional[str]) -> tuple:
        """Return (allowed, reason). Soft bias modes never hard-block; only *_only do."""
        if side is None:
            return False, "no_side"
        s = str(side).lower()
        mode = self.policy.side_mode
        if mode == "down_only" and s != "down":
            return False, "lane15m_down_only"
        if mode == "up_only" and s != "up":
            return False, "lane15m_up_only"
        return True, ""

    def side_size_mult(self, side: Optional[str]) -> float:
        """Soft bias: shrink opposed side size instead of hard block."""
        s = str(side or "").lower()
        mode = self.policy.side_mode
        if mode == "down_bias" and s == "up":
            return 0.40
        if mode == "up_bias" and s == "down":
            return 0.40
        return 1.0

    def timing_ok(self, sso: float, ttc_s: float) -> tuple:
        if sso < float(self.policy.min_sso):
            return False, "lane15m_sso_floor"
        if sso > float(self.policy.max_sso):
            return False, "lane15m_sso_ceiling"
        return True, ""

    def price_ok(self, ask: Optional[float]) -> tuple:
        if ask is None:
            return False, "no_ask"
        a = float(ask)
        if a < float(self.policy.min_entry_price):
            return False, "lane15m_min_entry"
        if a < float(self.policy.sweet_min) or a > float(self.policy.sweet_max):
            # Soft: allow but flag — engine may still take with size haircut
            return True, "outside_sweet"
        return True, ""

    def apply_to_tier_cfg(self, tier_cfg) -> None:
        """Push lane policy into a *copy* of tier cfg fields used for 15m evals.

        Callers should pass a dedicated TierConfig for 15m, not the shared hourly one,
        OR temporarily override and restore. Engine uses overlay helpers instead.
        """
        tier_cfg.sweet_min = float(self.policy.sweet_min)
        tier_cfg.sweet_max = float(self.policy.sweet_max)
        tier_cfg.min_seconds_since_open = float(self.policy.min_sso)
        tier_cfg.strike_edge_min = float(self.policy.strike_edge_min)
        tier_cfg.harvest_edge_min = float(self.policy.harvest_edge_min)

    # ---- persistence ----
    def to_state(self) -> dict:
        p = self.policy
        return {
            "enabled": bool(self.cfg.enabled),
            "policy": {
                "side_mode": p.side_mode,
                "min_sso": p.min_sso,
                "max_sso": p.max_sso,
                "prefer_ttc_min": p.prefer_ttc_min,
                "prefer_ttc_max": p.prefer_ttc_max,
                "sweet_min": p.sweet_min,
                "sweet_max": p.sweet_max,
                "min_edge": p.min_edge,
                "min_entry_price": p.min_entry_price,
                "probe_enabled": p.probe_enabled,
                "harvest_edge_min": p.harvest_edge_min,
                "strike_edge_min": p.strike_edge_min,
                "max_size_usd": p.max_size_usd,
            },
            "recent": list(self._recent),
            "since_adjust": int(self._since_adjust),
            "adjustments": list(self._adjustments[-20:]),
            "last_action": self._last_action,
            "last_ts": self._last_ts,
            "config": {
                "lookback_n": self.cfg.lookback_n,
                "min_samples": self.cfg.min_samples,
                "target_wr": self.cfg.target_wr,
                "kill_wr": self.cfg.kill_wr,
                "starve_fills_per_hour": self.cfg.starve_fills_per_hour,
                "cooldown_settlements": self.cfg.cooldown_settlements,
            },
        }

    def load_state(self, data: dict) -> None:
        if not data:
            return
        pol = data.get("policy") or {}
        for k, v in pol.items():
            if hasattr(self.policy, k):
                setattr(self.policy, k, v)
        self._recent = deque(list(data.get("recent") or []),
                             maxlen=max(16, int(self.cfg.lookback_n)))
        self._since_adjust = int(data.get("since_adjust") or 0)
        self._adjustments = list(data.get("adjustments") or [])[-40:]
        self._last_action = data.get("last_action")
        self._last_ts = data.get("last_ts")

    def report(self) -> dict:
        roll = self._rolling()
        p = self.policy
        return {
            "enabled": bool(self.cfg.enabled),
            "policy": {
                "side_mode": p.side_mode,
                "min_sso": p.min_sso,
                "max_sso": p.max_sso,
                "prefer_ttc": [p.prefer_ttc_min, p.prefer_ttc_max],
                "sweet": [p.sweet_min, p.sweet_max],
                "min_edge": p.min_edge,
                "min_entry_price": p.min_entry_price,
                "probe_enabled": p.probe_enabled,
            },
            "rolling": roll,
            "since_adjust": int(self._since_adjust),
            "last_action": self._last_action,
            "last_ts": self._last_ts,
            "recent_adjustments": list(self._adjustments[-5:]),
            "targets": {
                "target_wr": self.cfg.target_wr,
                "kill_wr": self.cfg.kill_wr,
                "starve_fills_per_hour": self.cfg.starve_fills_per_hour,
            },
            "note": ("15m lane learner: rewrites side/timing/sweet/edge from settled "
                     "outcomes to raise WR. Separate from hourly GateAutoTuner. PAPER ONLY."),
        }
