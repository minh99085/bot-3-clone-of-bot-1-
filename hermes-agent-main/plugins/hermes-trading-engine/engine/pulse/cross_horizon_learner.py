"""Cross-horizon shared learning (PAPER ONLY) — 15m ↔ 1h.

Grades settled directional fills into shared relative-timing buckets and publishes a
restrict/size-only policy both horizons consume. Does NOT add Loop Engineering lanes,
bypass maker-checker, or change MEMORY.md schema.

Transfer (evidence-backed):
  * 15m → 1h: mid-window relative SSO/TTC, both-side when DOWN proves, demote early-UP noise
  * 1h → 15m: demote patterns that bleed on 1h (e.g. early UP) on 15m too

Wilson-gated promote/demote; exploration carve-out avoids deadlock.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def _wilson_lb(wins: int, n: int, z: float = 1.64) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    spread = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return max(0.0, (centre - spread) / denom)


def classify_horizon(window_seconds: Optional[float] = None,
                     series_slug: Optional[str] = None,
                     market_series: Optional[str] = None) -> str:
    slug = f"{series_slug or ''} {market_series or ''}".lower()
    try:
        ws = float(window_seconds) if window_seconds is not None else 0.0
    except (TypeError, ValueError):
        ws = 0.0
    if "15m" in slug or 600 <= ws <= 1200:
        return "15m"
    if "hour" in slug or "1h" in slug or ws >= 3000:
        return "1h"
    if ws >= 600:
        return "15m"
    return "other"


def sso_frac(sso: Optional[float], window_seconds: float) -> Optional[float]:
    ws = float(window_seconds or 0)
    if ws <= 0 or sso is None:
        return None
    return _clamp(float(sso) / ws, 0.0, 1.0)


def ttc_frac(ttc_s: Optional[float], window_seconds: float) -> Optional[float]:
    ws = float(window_seconds or 0)
    if ws <= 0 or ttc_s is None:
        return None
    return _clamp(float(ttc_s) / ws, 0.0, 1.0)


def entry_band(price: Optional[float]) -> str:
    if price is None:
        return "na"
    p = float(price)
    if p < 0.50:
        return "lt_50"
    if p < 0.55:
        return "50_55"
    if p < 0.70:
        return "55_70"
    return "70_plus"


def timing_band(frac: Optional[float]) -> str:
    if frac is None:
        return "na"
    f = float(frac)
    if f < 0.15:
        return "early"
    if f < 0.45:
        return "mid"
    return "late"


@dataclass
class CrossHorizonPolicy:
    """Shared overlays both horizons read (restrict / size only)."""

    # 1h overlays learned from 15m mid-window winners
    h1_min_sso_frac: float = 0.0          # 0 = off; else require SSO/ws >= this
    h1_prefer_down: bool = False          # soft: size UP haircut when set
    h1_block_early_up: bool = False       # demote early UP on 1h
    h1_up_size_mult: float = 1.0
    h1_down_size_mult: float = 1.0
    # 15m overlays learned from 1h bleed patterns
    m15_block_early_up: bool = False
    m15_up_size_mult: float = 1.0
    m15_down_size_mult: float = 1.0
    # Bookkeeping
    promoted: list = field(default_factory=list)
    demoted: list = field(default_factory=list)
    note: str = "cross-horizon shared policy — restrict/size only"


@dataclass
class CrossHorizonConfig:
    enabled: bool = True
    lookback_n: int = 120
    min_samples: int = 20          # per-horizon before transfer fires
    min_bucket_n: int = 8
    target_wr: float = 0.60
    kill_wr: float = 0.45
    breakeven_wr: float = 0.55
    exploration_rate: float = 0.08
    cooldown_settlements: int = 6
    # Transfer targets from 15m mid winners → 1h floor
    transfer_sso_frac_lo: float = 0.15
    transfer_sso_frac_hi: float = 0.45


class CrossHorizonLearner:
    """Shared graded policy: 15m ↔ 1h evidence → restrict/size overlays."""

    def __init__(self, cfg: Optional[CrossHorizonConfig] = None,
                 policy: Optional[CrossHorizonPolicy] = None):
        self.cfg = cfg or CrossHorizonConfig()
        self.policy = policy or CrossHorizonPolicy()
        self._recent: Deque[dict] = deque(maxlen=max(32, int(self.cfg.lookback_n)))
        self._since_adjust = 0
        self._adjustments: list = []
        self._last_action: Optional[str] = None
        self._last_ts: Optional[float] = None
        self._blocked = 0
        self._explored = 0
        self._passed = 0

    # ---- evidence ----
    def record_settled(
        self,
        *,
        won: bool,
        pnl_usd: float,
        horizon: str,
        side: Optional[str] = None,
        entry_price: Optional[float] = None,
        window_seconds: Optional[float] = None,
        sso: Optional[float] = None,
        ttc_s: Optional[float] = None,
        entry_mode: Optional[str] = None,
        asset: str = "btc",
        chart_alignment: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        if not self.cfg.enabled:
            return
        h = str(horizon or "other").lower()
        if h not in ("15m", "1h"):
            return
        ws = float(window_seconds or (900.0 if h == "15m" else 3600.0))
        sf = sso_frac(sso, ws)
        tf = ttc_frac(ttc_s, ws)
        row = {
            "ts": float(now if now is not None else time.time()),
            "won": bool(won),
            "pnl": float(pnl_usd or 0.0),
            "horizon": h,
            "side": (str(side).lower() if side else None),
            "entry": float(entry_price) if entry_price is not None else None,
            "entry_band": entry_band(entry_price),
            "sso_frac": sf,
            "ttc_frac": tf,
            "timing": timing_band(sf),
            "mode": str(entry_mode or "") or None,
            "asset": str(asset or "btc").lower(),
            "lean": str(chart_alignment or "") or None,
            "ws": ws,
        }
        self._recent.append(row)
        self._since_adjust += 1

    def _bucket_stats(self, rows: list) -> dict:
        n = len(rows)
        wins = sum(1 for r in rows if r.get("won"))
        pnl = sum(float(r.get("pnl") or 0) for r in rows)
        return {
            "n": n,
            "wins": wins,
            "win_rate": (wins / n) if n else None,
            "wilson_lb": _wilson_lb(wins, n),
            "pnl_usd": round(pnl, 4),
        }

    def _rows(self, horizon: Optional[str] = None) -> list:
        rows = list(self._recent)
        if horizon:
            rows = [r for r in rows if r.get("horizon") == horizon]
        return rows

    def _group(self, rows: list, key_fn) -> dict:
        out: dict = {}
        for r in rows:
            k = key_fn(r)
            if k is None:
                continue
            out.setdefault(k, []).append(r)
        return {k: self._bucket_stats(v) for k, v in out.items()}

    def maybe_adjust(self, now: Optional[float] = None) -> Optional[str]:
        """Rewrite CrossHorizonPolicy from graded evidence. Returns action or None."""
        if not self.cfg.enabled:
            return None
        if self._since_adjust < int(self.cfg.cooldown_settlements):
            return None
        rows_15 = self._rows("15m")
        rows_1h = self._rows("1h")
        if len(rows_15) < int(self.cfg.min_samples) and len(rows_1h) < int(self.cfg.min_samples):
            return None

        promoted: list = []
        demoted: list = []
        action_parts: list = []
        pol = CrossHorizonPolicy()

        # --- 15m → 1h: mid-window relative timing + side transfer ---
        mid_15 = [r for r in rows_15 if r.get("timing") == "mid"]
        mid_stats = self._bucket_stats(mid_15)
        if mid_stats["n"] >= int(self.cfg.min_bucket_n) and (
                mid_stats["wilson_lb"] >= float(self.cfg.breakeven_wr)):
            # Push 1h to enter no earlier than 15m mid band floor
            pol.h1_min_sso_frac = float(self.cfg.transfer_sso_frac_lo)
            promoted.append({
                "bucket": "15m_mid_timing",
                **mid_stats,
                "transfer": "1h_min_sso_frac",
            })
            action_parts.append("promote_15m_mid→1h_sso")

        down_15 = [r for r in rows_15 if r.get("side") == "down"]
        up_15 = [r for r in rows_15 if r.get("side") == "up"]
        down_s = self._bucket_stats(down_15)
        up_s = self._bucket_stats(up_15)
        if (down_s["n"] >= int(self.cfg.min_bucket_n)
                and down_s["wilson_lb"] >= float(self.cfg.target_wr)
                and (up_s["n"] < int(self.cfg.min_bucket_n)
                     or down_s["wilson_lb"] > (up_s["wilson_lb"] + 0.05))):
            pol.h1_prefer_down = True
            pol.h1_down_size_mult = 1.15
            pol.h1_up_size_mult = 0.85
            promoted.append({"bucket": "15m_down", **down_s, "transfer": "1h_prefer_down"})
            action_parts.append("promote_15m_down→1h")

        early_up_15 = [r for r in rows_15
                       if r.get("side") == "up" and r.get("timing") == "early"]
        eu15 = self._bucket_stats(early_up_15)
        if eu15["n"] >= int(self.cfg.min_bucket_n) and eu15["wilson_lb"] < float(self.cfg.kill_wr):
            pol.m15_block_early_up = True
            pol.m15_up_size_mult = 0.75
            demoted.append({"bucket": "15m_early_up", **eu15, "transfer": "15m_block_early_up"})
            action_parts.append("demote_15m_early_up")

        # --- 1h → 15m: if 1h UP bleeds, demote early UP on both ---
        up_1h = [r for r in rows_1h if r.get("side") == "up"]
        u1 = self._bucket_stats(up_1h)
        if u1["n"] >= max(4, int(self.cfg.min_bucket_n) // 2) and u1["wilson_lb"] < float(
                self.cfg.kill_wr):
            pol.h1_block_early_up = True
            pol.h1_up_size_mult = min(pol.h1_up_size_mult, 0.70)
            pol.m15_block_early_up = True
            pol.m15_up_size_mult = min(pol.m15_up_size_mult, 0.80)
            demoted.append({"bucket": "1h_up", **u1, "transfer": "both_demote_early_up"})
            action_parts.append("demote_1h_up→both")

        early_1h = [r for r in rows_1h if r.get("timing") == "early"]
        e1 = self._bucket_stats(early_1h)
        if e1["n"] >= max(4, int(self.cfg.min_bucket_n) // 2) and e1["wilson_lb"] < float(
                self.cfg.kill_wr):
            # Strengthen 1h SSO floor even if 15m mid not yet proven
            pol.h1_min_sso_frac = max(pol.h1_min_sso_frac, float(self.cfg.transfer_sso_frac_lo))
            demoted.append({"bucket": "1h_early", **e1, "transfer": "1h_min_sso_frac"})
            action_parts.append("demote_1h_early")

        if not action_parts:
            # Still refresh bookkeeping so report shows graded buckets
            self.policy.promoted = []
            self.policy.demoted = []
            self._since_adjust = 0
            self._last_action = "hold"
            self._last_ts = float(now if now is not None else time.time())
            return "hold"

        pol.promoted = promoted[-12:]
        pol.demoted = demoted[-12:]
        pol.note = "cross-horizon shared policy — restrict/size only; execution gate authoritative"
        self.policy = pol
        self._since_adjust = 0
        action = "+".join(action_parts)
        self._last_action = action
        self._last_ts = float(now if now is not None else time.time())
        self._adjustments.append({
            "ts": self._last_ts,
            "action": action,
            "policy": self.to_state()["policy"],
            "n_15m": len(rows_15),
            "n_1h": len(rows_1h),
        })
        self._adjustments = self._adjustments[-40:]
        return action

    # ---- consume (restrict / size only) ----
    def evaluate_entry(
        self,
        *,
        horizon: str,
        side: str,
        sso: Optional[float],
        ttc_s: Optional[float],
        window_seconds: float,
        explore_rng=None,
    ) -> dict:
        """Return {decision, reason, size_mult}. decision: pass|reject|explore."""
        if not self.cfg.enabled:
            return {"decision": "pass", "reason": "disabled", "size_mult": 1.0}
        h = str(horizon or "").lower()
        side_l = str(side or "").lower()
        ws = float(window_seconds or (900.0 if h == "15m" else 3600.0))
        sf = sso_frac(sso, ws)
        pol = self.policy
        size_mult = 1.0
        reasons: list = []

        if h == "1h":
            if pol.h1_min_sso_frac > 0 and sf is not None and sf < float(pol.h1_min_sso_frac):
                reasons.append("xh_1h_sso_frac_floor")
            if pol.h1_block_early_up and side_l == "up" and sf is not None and sf < 0.15:
                reasons.append("xh_1h_block_early_up")
            if side_l == "up":
                size_mult *= float(pol.h1_up_size_mult)
            elif side_l == "down":
                size_mult *= float(pol.h1_down_size_mult)
        elif h == "15m":
            if pol.m15_block_early_up and side_l == "up" and sf is not None and sf < 0.15:
                reasons.append("xh_15m_block_early_up")
            if side_l == "up":
                size_mult *= float(pol.m15_up_size_mult)
            elif side_l == "down":
                size_mult *= float(pol.m15_down_size_mult)

        if reasons:
            # exploration carve-out
            import random
            rng = explore_rng if explore_rng is not None else random
            if float(rng.random()) < float(self.cfg.exploration_rate):
                self._explored += 1
                return {
                    "decision": "explore",
                    "reason": reasons[0],
                    "size_mult": max(0.25, min(1.5, size_mult)),
                    "reasons": reasons,
                }
            self._blocked += 1
            return {
                "decision": "reject",
                "reason": reasons[0],
                "size_mult": 1.0,
                "reasons": reasons,
            }

        self._passed += 1
        return {
            "decision": "pass",
            "reason": "",
            "size_mult": max(0.25, min(1.5, size_mult)),
            "reasons": [],
        }

    # ---- persistence / report ----
    def to_state(self) -> dict:
        p = self.policy
        return {
            "enabled": bool(self.cfg.enabled),
            "policy": {
                "h1_min_sso_frac": p.h1_min_sso_frac,
                "h1_prefer_down": p.h1_prefer_down,
                "h1_block_early_up": p.h1_block_early_up,
                "h1_up_size_mult": p.h1_up_size_mult,
                "h1_down_size_mult": p.h1_down_size_mult,
                "m15_block_early_up": p.m15_block_early_up,
                "m15_up_size_mult": p.m15_up_size_mult,
                "m15_down_size_mult": p.m15_down_size_mult,
                "promoted": list(p.promoted),
                "demoted": list(p.demoted),
                "note": p.note,
            },
            "recent": list(self._recent),
            "since_adjust": int(self._since_adjust),
            "adjustments": list(self._adjustments[-20:]),
            "last_action": self._last_action,
            "last_ts": self._last_ts,
            "counters": {
                "blocked": self._blocked,
                "explored": self._explored,
                "passed": self._passed,
            },
            "config": {
                "lookback_n": self.cfg.lookback_n,
                "min_samples": self.cfg.min_samples,
                "min_bucket_n": self.cfg.min_bucket_n,
                "target_wr": self.cfg.target_wr,
                "kill_wr": self.cfg.kill_wr,
                "exploration_rate": self.cfg.exploration_rate,
                "transfer_sso_frac_lo": self.cfg.transfer_sso_frac_lo,
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
                             maxlen=max(32, int(self.cfg.lookback_n)))
        self._since_adjust = int(data.get("since_adjust") or 0)
        self._adjustments = list(data.get("adjustments") or [])[-40:]
        self._last_action = data.get("last_action")
        self._last_ts = data.get("last_ts")
        ctr = data.get("counters") or {}
        self._blocked = int(ctr.get("blocked") or 0)
        self._explored = int(ctr.get("explored") or 0)
        self._passed = int(ctr.get("passed") or 0)

    def report(self) -> dict:
        rows_15 = self._rows("15m")
        rows_1h = self._rows("1h")
        p = self.policy
        return {
            "enabled": bool(self.cfg.enabled),
            "affects_trading": bool(self.cfg.enabled),
            "can_force_trade": False,
            "mode": "restrict_size_only_shared_policy",
            "execution_gate_still_authoritative": True,
            "policy": {
                "h1_min_sso_frac": p.h1_min_sso_frac,
                "h1_prefer_down": p.h1_prefer_down,
                "h1_block_early_up": p.h1_block_early_up,
                "h1_up_size_mult": p.h1_up_size_mult,
                "h1_down_size_mult": p.h1_down_size_mult,
                "m15_block_early_up": p.m15_block_early_up,
                "m15_up_size_mult": p.m15_up_size_mult,
                "m15_down_size_mult": p.m15_down_size_mult,
            },
            "promoted": list(p.promoted)[-8:],
            "demoted": list(p.demoted)[-8:],
            "rolling": {
                "15m": self._bucket_stats(rows_15),
                "1h": self._bucket_stats(rows_1h),
                "by_timing_15m": self._group(rows_15, lambda r: r.get("timing")),
                "by_side_15m": self._group(rows_15, lambda r: r.get("side")),
                "by_timing_1h": self._group(rows_1h, lambda r: r.get("timing")),
                "by_side_1h": self._group(rows_1h, lambda r: r.get("side")),
            },
            "counters": {
                "blocked": self._blocked,
                "explored": self._explored,
                "passed": self._passed,
            },
            "last_action": self._last_action,
            "last_ts": self._last_ts,
            "recent_adjustments": list(self._adjustments[-5:]),
            "note": (
                "Shared cross-horizon learner: 15m↔1h graded buckets drive restrict/size "
                "overlays only. Locked — change only with operator approval."
            ),
        }
