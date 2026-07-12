"""Directional p_exec — fill-conditioned win probability + context self-tune.

ONE probability object for directional EV gate:
  p_exec(c) = (1-w)*p_blend + w*WR_emp(c)

p_blend blends market mid, digital fair, and Grok-param MC (weights from Brier).
Contexts promote/demote from settled economics (Wilson + PnL). PAPER ONLY.
"""

from __future__ import annotations

import math
from typing import Optional


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def context_key(*, asset: str, horizon: str, side: str = "unknown", ttc_s: float, vwap: float,
                sso_s: Optional[float] = None, lead_state: str = "none") -> str:
    """Compact context key (≤6 dims) for promote/demote."""
    asset = str(asset or "btc").lower()
    if asset not in ("btc", "eth"):
        asset = "btc"
    horizon = str(horizon or "15m").lower()
    if horizon not in ("15m", "1h", "5m"):
        ws = horizon
        if "3600" in ws or "1h" in ws:
            horizon = "1h"
        elif "900" in ws or "15" in ws:
            horizon = "15m"
        else:
            horizon = "15m"
    ttc = float(ttc_s or 0)
    if horizon == "1h":
        if ttc >= 2700:
            ttc_b = "early"
        elif ttc >= 900:
            ttc_b = "mid"
        else:
            ttc_b = "late"
    else:
        if ttc >= 600:
            ttc_b = "early"
        elif ttc >= 180:
            ttc_b = "mid"
        else:
            ttc_b = "late"
    v = float(vwap or 0)
    if v < 0.50:
        vwap_b = "lt50"
    elif v < 0.55:
        vwap_b = "50_55"
    elif v < 0.65:
        vwap_b = "55_65"
    elif v < 0.80:
        vwap_b = "65_80"
    else:
        vwap_b = "ge80"
    sso = float(sso_s or 0)
    if horizon == "1h":
        if sso < 900:
            sso_b = "h0_15"
        elif sso < 1800:
            sso_b = "h15_30"
        elif sso < 2700:
            sso_b = "h30_45"
        else:
            sso_b = "h45_60"
    else:
        sso_b = "na"
    lead = str(lead_state or "none").lower()
    if lead not in ("cex_agree", "cex_diverge", "none"):
        lead = "none"
    side = str(side or "unknown").lower()
    if side not in ("up", "down"):
        side = "unknown"
    return f"{asset}|{horizon}|{side}|{ttc_b}|{vwap_b}|{sso_b}|{lead}"


def wilson_lb(wins: int, n: int, z: float = 1.64) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    spread = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return max(0.0, (centre - spread) / denom)


def wilson_ub(wins: int, n: int, z: float = 1.64) -> float:
    if n <= 0:
        return 1.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    spread = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return min(1.0, (centre + spread) / denom)


def blend_p(*, p_mkt: Optional[float], p_digital: Optional[float],
            p_mc: Optional[float], w_mkt: float = 0.34, w_dig: float = 0.33,
            w_mc: float = 0.33) -> Optional[float]:
    """Weighted blend of available probability sources (renormalize missing)."""
    parts = []
    for p, w in ((p_mkt, w_mkt), (p_digital, w_dig), (p_mc, w_mc)):
        if p is None:
            continue
        try:
            parts.append((_clip01(float(p)), max(0.0, float(w))))
        except (TypeError, ValueError):
            continue
    if not parts:
        return None
    tw = sum(w for _, w in parts)
    if tw <= 0:
        return _clip01(sum(p for p, _ in parts) / len(parts))
    return _clip01(sum(p * w for p, w in parts) / tw)


def compute_p_exec(*, p_blend: Optional[float], wr_emp: Optional[float],
                   n_c: int = 0, n0: float = 40.0, w_max: float = 0.7) -> Optional[float]:
    """Shrink p_blend toward empirical WR as context sample grows."""
    if p_blend is None and wr_emp is None:
        return None
    if p_blend is None:
        return _clip01(float(wr_emp))
    if wr_emp is None or n_c <= 0:
        return _clip01(float(p_blend))
    w = min(float(w_max), float(n_c) / (float(n_c) + float(n0)))
    return _clip01((1.0 - w) * float(p_blend) + w * float(wr_emp))


class ContextSelfTune:
    """Per-context promote/demote from settled directional fills."""

    def __init__(self, *, min_promote_n: int = 40, min_demote_n: int = 30,
                 margin: float = 0.02, z: float = 1.64, explore_rate: float = 0.05):
        self.min_promote_n = int(min_promote_n)
        self.min_demote_n = int(min_demote_n)
        self.margin = float(margin)
        self.z = float(z)
        self.explore_rate = max(0.0, min(0.15, float(explore_rate)))
        self.buckets: dict = {}
        self.promoted: set = set()
        self.demoted: set = set()
        self.w_mc = 0.0  # soft blend weight for MC (raised when MC Brier wins)
        self.mc_brier_n = 0
        self.mc_brier_sum = 0.0
        self.mkt_brier_sum = 0.0
        self.dig_brier_sum = 0.0

    @staticmethod
    def _stat() -> dict:
        return {"n": 0, "wins": 0, "pnl": 0.0, "vwap_sum": 0.0,
                "brier_blend": 0.0, "brier_mkt": 0.0}

    def record(self, key: str, *, won: bool, pnl: float, vwap: float,
               p_blend: Optional[float] = None, p_mkt: Optional[float] = None,
               p_mc: Optional[float] = None, p_digital: Optional[float] = None) -> None:
        k = str(key or "")
        if not k:
            return
        s = self.buckets.setdefault(k, self._stat())
        s["n"] += 1
        s["wins"] += int(bool(won))
        s["pnl"] = round(s["pnl"] + float(pnl or 0.0), 6)
        s["vwap_sum"] = round(s["vwap_sum"] + float(vwap or 0.0), 6)
        y = 1.0 if won else 0.0
        if p_blend is not None:
            s["brier_blend"] = round(s["brier_blend"] + (_clip01(p_blend) - y) ** 2, 6)
        if p_mkt is not None:
            s["brier_mkt"] = round(s["brier_mkt"] + (_clip01(p_mkt) - y) ** 2, 6)
        # Global MC vs market Brier for w_mc auto-tune
        if p_mc is not None and p_mkt is not None:
            self.mc_brier_n += 1
            self.mc_brier_sum += (_clip01(p_mc) - y) ** 2
            self.mkt_brier_sum += (_clip01(p_mkt) - y) ** 2
            if p_digital is not None:
                self.dig_brier_sum += (_clip01(p_digital) - y) ** 2
            self._retune_w_mc()
        self._reclassify(k)

    def _retune_w_mc(self) -> None:
        n = self.mc_brier_n
        if n < 30:
            self.w_mc = 0.0
            return
        mc_b = self.mc_brier_sum / n
        mkt_b = self.mkt_brier_sum / n
        # Raise w_mc only when MC beats market Brier
        if mc_b < mkt_b - 0.005:
            self.w_mc = min(0.40, 0.10 + 0.30 * min(1.0, (mkt_b - mc_b) / 0.05))
        else:
            self.w_mc = 0.0

    def _reclassify(self, key: str) -> None:
        s = self.buckets.get(key)
        if not s or s["n"] <= 0:
            return
        n = int(s["n"])
        wins = int(s["wins"])
        be = (s["vwap_sum"] / n) if n else 0.5
        lb = wilson_lb(wins, n, self.z)
        ub = wilson_ub(wins, n, self.z)
        pnl = float(s["pnl"])
        if n >= self.min_promote_n and lb > be + self.margin and pnl > 0:
            self.promoted.add(key)
            self.demoted.discard(key)
        elif n >= self.min_demote_n and (ub < be or pnl < 0):
            self.demoted.add(key)
            self.promoted.discard(key)

    def is_promoted(self, key: str) -> bool:
        return str(key) in self.promoted

    def is_demoted(self, key: str) -> bool:
        return str(key) in self.demoted

    def wr_emp(self, key: str) -> Optional[float]:
        s = self.buckets.get(str(key))
        if not s or s["n"] <= 0:
            return None
        return round(s["wins"] / s["n"], 6)

    def n_c(self, key: str) -> int:
        s = self.buckets.get(str(key))
        return int(s["n"]) if s else 0

    def allow_trade(self, key: str, *, rng=None) -> tuple[bool, str]:
        """Return (ok, reason). Demoted blocked; unpromoted explore at capped rate."""
        k = str(key)
        if k in self.demoted:
            return False, "context_demoted"
        if k in self.promoted:
            return True, "context_promoted"
        # explore
        import random
        r = rng if rng is not None else random
        if self.explore_rate > 0 and r.random() < self.explore_rate:
            return True, "context_explore"
        return False, "context_cold"

    def blend_weights(self) -> dict:
        """Dynamic weights for p_blend (MC weight from self-tune)."""
        w_mc = float(self.w_mc)
        rem = 1.0 - w_mc
        return {"w_mkt": round(rem * 0.5, 4), "w_dig": round(rem * 0.5, 4),
                "w_mc": round(w_mc, 4)}

    def report(self) -> dict:
        rows = []
        for k, s in self.buckets.items():
            n = s["n"]
            if n <= 0:
                continue
            rows.append({
                "key": k, "n": n, "wr": round(s["wins"] / n, 4),
                "pnl": round(s["pnl"], 4),
                "mean_vwap": round(s["vwap_sum"] / n, 4),
                "promoted": k in self.promoted,
                "demoted": k in self.demoted,
            })
        rows.sort(key=lambda r: (-int(r["promoted"]), -r["n"]))
        return {
            "promoted_n": len(self.promoted),
            "demoted_n": len(self.demoted),
            "buckets": rows[:20],
            "w_mc": self.w_mc,
            "mc_brier_n": self.mc_brier_n,
            "mc_brier": (round(self.mc_brier_sum / self.mc_brier_n, 4)
                         if self.mc_brier_n else None),
            "mkt_brier": (round(self.mkt_brier_sum / self.mc_brier_n, 4)
                          if self.mc_brier_n else None),
        }

    def to_state(self) -> dict:
        return {
            "buckets": {k: dict(v) for k, v in self.buckets.items()},
            "promoted": sorted(self.promoted),
            "demoted": sorted(self.demoted),
            "w_mc": self.w_mc,
            "mc_brier_n": self.mc_brier_n,
            "mc_brier_sum": self.mc_brier_sum,
            "mkt_brier_sum": self.mkt_brier_sum,
            "dig_brier_sum": self.dig_brier_sum,
        }

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.buckets = {}
        for k, s in (data.get("buckets") or {}).items():
            st = self._stat()
            for key in st:
                if key in ("n", "wins"):
                    st[key] = int(s.get(key, 0) or 0)
                else:
                    st[key] = float(s.get(key, 0.0) or 0.0)
            self.buckets[str(k)] = st
        self.promoted = set(str(x) for x in (data.get("promoted") or []))
        self.demoted = set(str(x) for x in (data.get("demoted") or []))
        self.w_mc = float(data.get("w_mc", 0.0) or 0.0)
        self.mc_brier_n = int(data.get("mc_brier_n", 0) or 0)
        self.mc_brier_sum = float(data.get("mc_brier_sum", 0.0) or 0.0)
        self.mkt_brier_sum = float(data.get("mkt_brier_sum", 0.0) or 0.0)
        self.dig_brier_sum = float(data.get("dig_brier_sum", 0.0) or 0.0)
