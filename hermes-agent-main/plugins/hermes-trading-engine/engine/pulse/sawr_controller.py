"""Self-Adjusting Win-Rate (SAWR) controller — invented meta-learner (PAPER ONLY).

Research finding: Bot-3 has many uncoordinated learners (GateAutoTuner, Lane15m,
CrossHorizon, Selectivity, BinaryIntel) that nudge overlapping knobs without a shared
objective. SAWR is the missing meta-layer.

Invented method
---------------
1. **Fill-Quality Pareto utility** over rolling settlements:

       U = w_wr · Wilson_LB(WR, n) + w_fill · log(1 + fills/h)
           − λ · max(0, kill_wr − WR)

   Wilson LB is a conservative WR estimator (avoids over-reacting to lucky streaks).

2. **Empirical-Bayes side affinity** — Beta(α, β) posteriors per (asset, lane, side).
   At decision time, size_mult and soft_block follow posterior mean vs ask (edge).

3. **Adaptive step shrinkage** — η_t = η₀ / (1 + √n_adj) · regime_factor, where
   regime_factor < 1 when rolling Brier of model P(win) degrades vs a market mid baseline.

4. **Conflict arbitration** — if WR is below kill floor, veto loosen proposals from
   subordinate tuners (stance=veto_loosen). If starved on fills with healthy Wilson LB,
   allow coordinated loosen.

Does NOT replace Binary Intel (per-trade math) or GateAutoTuner (hourly scalar nudges).
SAWR arbitrates and adds side-affinity sizing. Execution gate remains fill authority.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


def wilson_lower(wins: int, n: int, z: float = 1.645) -> float:
    """One-sided Wilson score lower bound (default ~95% one-sided)."""
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return max(0.0, center - margin)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def lane_from_research(research: Optional[dict]) -> str:
    rt = research or {}
    slug = str(rt.get("series_slug") or rt.get("market_series") or "").lower()
    ws = int(rt.get("window_seconds") or 0)
    if ws >= 3600 or "1h" in slug or "hourly" in slug:
        return "1h"
    if ws >= 600 or "15m" in slug:
        return "15m"
    return "5m"


def asset_from_research(research: Optional[dict]) -> str:
    rt = research or {}
    slug = str(rt.get("series_slug") or "").lower()
    if "eth" in slug or "ethereum" in slug:
        return "eth"
    return "btc"


@dataclass
class SawrConfig:
    enabled: bool = True
    lookback_n: int = 40
    min_samples: int = 8
    target_wr: float = 0.60
    kill_wr: float = 0.48
    starve_fph: float = 0.6
    rich_fph: float = 4.0
    wr_weight: float = 1.0
    fill_weight: float = 0.35
    kill_penalty: float = 2.0
    cooldown_settlements: int = 5
    step_edge: float = 0.004
    step_entry: float = 0.01
    step_ev: float = 0.002
    # Training-compatible floors (align with loosened paper profile).
    min_edge_lo: float = 0.005
    min_edge_hi: float = 0.08
    min_entry_lo: float = 0.35
    min_entry_hi: float = 0.62
    exec_ev_lo: float = 0.0
    exec_ev_hi: float = 0.025
    # Empirical Bayes priors (weakly informative ~55% WR prior).
    beta_alpha0: float = 5.5
    beta_beta0: float = 4.5
    side_min_n: int = 6
    soft_block_edge: float = 0.02
    size_boost_max: float = 1.25
    size_cut_min: float = 0.45
    brier_degrade_delta: float = 0.02


@dataclass
class BetaAffinity:
    alpha: float = 5.5
    beta: float = 4.5

    @property
    def n(self) -> float:
        return max(0.0, self.alpha + self.beta - 11.0)  # approx after prior

    @property
    def mean(self) -> float:
        s = self.alpha + self.beta
        return self.alpha / s if s > 0 else 0.5

    def update(self, won: bool) -> None:
        if won:
            self.alpha += 1.0
        else:
            self.beta += 1.0

    def samples(self, alpha0: float, beta0: float) -> int:
        return max(0, int(round(self.alpha + self.beta - alpha0 - beta0)))


class SawrController:
    """Meta-controller: Pareto WR utility + Beta side affinity + adaptive shrink."""

    def __init__(self, cfg: Optional[SawrConfig] = None):
        self.cfg = cfg or SawrConfig()
        self._recent: Deque[dict] = deque(maxlen=max(12, int(self.cfg.lookback_n)))
        self._since_adjust = 0
        self._n_adjustments = 0
        self._stance: str = "hold"
        self._last_action: Optional[str] = None
        self._last_utility: Optional[float] = None
        self._last_ts: Optional[float] = None
        self._adjustments: list = []
        self._affinity: dict[str, BetaAffinity] = {}
        self._model_brier_sum = 0.0
        self._mkt_brier_sum = 0.0
        self._brier_n = 0
        self._regime_factor = 1.0
        self._veto_loosen = False

    def _aff_key(self, asset: str, lane: str, side: str) -> str:
        return f"{asset}|{lane}|{str(side or '').lower()}"

    def _get_aff(self, asset: str, lane: str, side: str) -> BetaAffinity:
        k = self._aff_key(asset, lane, side)
        if k not in self._affinity:
            self._affinity[k] = BetaAffinity(
                alpha=float(self.cfg.beta_alpha0),
                beta=float(self.cfg.beta_beta0),
            )
        return self._affinity[k]

    # ---- evidence ----
    def record_settled(
        self,
        *,
        won: bool,
        pnl_usd: float,
        side: Optional[str] = None,
        asset: str = "btc",
        lane: str = "15m",
        entry_price: Optional[float] = None,
        model_p_win: Optional[float] = None,
        market_mid: Optional[float] = None,
        now: Optional[float] = None,
    ) -> None:
        if not self.cfg.enabled:
            return
        ts = float(now if now is not None else time.time())
        a = str(asset or "btc").lower()
        ln = str(lane or "15m").lower()
        sd = str(side or "").lower()
        self._recent.append({
            "won": bool(won),
            "pnl": float(pnl_usd or 0.0),
            "side": sd,
            "asset": a,
            "lane": ln,
            "entry_price": float(entry_price) if entry_price is not None else None,
            "settled_ts": ts,
        })
        self._since_adjust += 1
        if sd in ("up", "down"):
            self._get_aff(a, ln, sd).update(bool(won))

        # Rolling Brier: model vs market mid (when both present).
        if model_p_win is not None:
            try:
                y = 1.0 if won else 0.0
                mp = float(model_p_win)
                self._model_brier_sum += (mp - y) ** 2
                if market_mid is not None:
                    mk = float(market_mid)
                    self._mkt_brier_sum += (mk - y) ** 2
                self._brier_n += 1
            except (TypeError, ValueError):
                pass

        self._refresh_regime_factor()
        # Keep stance/veto fresh even between cooldown windows (for GateAutoTuner arbitration).
        self.refresh_stance()

    def refresh_stance(self) -> str:
        """Update stance + veto_loosen from rolling evidence without applying cfg changes."""
        if not self.cfg.enabled:
            self._stance = "hold"
            self._veto_loosen = False
            return self._stance
        self._decide(self._rolling())
        return self._stance

    def _refresh_regime_factor(self) -> None:
        n = int(self._brier_n)
        if n < 20:
            self._regime_factor = 1.0
            return
        mb = self._model_brier_sum / n
        mk = self._mkt_brier_sum / n if self._mkt_brier_sum > 0 else mb
        # Model worse than market by degrade_delta → shrink step sizes.
        if mb > mk + float(self.cfg.brier_degrade_delta):
            self._regime_factor = 0.5
        elif mb > mk:
            self._regime_factor = 0.75
        else:
            self._regime_factor = 1.0

    def _rolling(self) -> dict:
        rows = list(self._recent)
        n = len(rows)
        if n == 0:
            return {"n": 0, "wins": 0, "win_rate": None, "wilson_lb": 0.0,
                    "pnl_usd": 0.0, "fills_per_hour": 0.0, "by_lane": {}}
        wins = sum(1 for r in rows if r["won"])
        pnl = sum(float(r["pnl"]) for r in rows)
        t0 = min(float(r["settled_ts"]) for r in rows)
        t1 = max(float(r["settled_ts"]) for r in rows)
        hours = max(1.0 / 60.0, (t1 - t0) / 3600.0) if n >= 2 else 1.0
        wr = wins / n
        by_lane: dict = {}
        for r in rows:
            k = f"{r['asset']}|{r['lane']}"
            st = by_lane.setdefault(k, {"n": 0, "wins": 0, "pnl_usd": 0.0})
            st["n"] += 1
            if r["won"]:
                st["wins"] += 1
            st["pnl_usd"] = round(st["pnl_usd"] + float(r["pnl"]), 4)
        for st in by_lane.values():
            st["win_rate"] = round(st["wins"] / st["n"], 4) if st["n"] else None
            st["wilson_lb"] = round(wilson_lower(st["wins"], st["n"]), 4)
        return {
            "n": n,
            "wins": wins,
            "win_rate": round(wr, 4),
            "wilson_lb": round(wilson_lower(wins, n), 4),
            "pnl_usd": round(pnl, 4),
            "fills_per_hour": round(n / hours, 4),
            "by_lane": by_lane,
            "span_hours": round(hours, 3),
        }

    def utility(self, roll: Optional[dict] = None) -> float:
        """Fill-Quality Pareto utility (higher is better)."""
        r = roll if roll is not None else self._rolling()
        n = int(r.get("n") or 0)
        if n <= 0:
            return 0.0
        wr = float(r.get("win_rate") or 0.0)
        wlb = float(r.get("wilson_lb") or 0.0)
        fph = float(r.get("fills_per_hour") or 0.0)
        u = (float(self.cfg.wr_weight) * wlb
             + float(self.cfg.fill_weight) * math.log1p(fph)
             - float(self.cfg.kill_penalty) * max(0.0, float(self.cfg.kill_wr) - wr))
        return round(u, 6)

    def _decide(self, roll: dict) -> Optional[str]:
        """Return 'tighten' | 'loosen' | None. Sets stance + veto flag."""
        n = int(roll.get("n") or 0)
        wr = roll.get("win_rate")
        wlb = float(roll.get("wilson_lb") or 0.0)
        fph = float(roll.get("fills_per_hour") or 0.0)
        self._veto_loosen = False

        if n < int(self.cfg.min_samples):
            # Early starvation: allow mild loosen if almost no fills.
            if n >= 3 and fph < float(self.cfg.starve_fph) * 0.4:
                self._stance = "explore_loosen"
                return "loosen"
            self._stance = "hold"
            return None

        wr_f = float(wr) if wr is not None else 0.0

        # Kill floor → always tighten; veto any subordinate loosen.
        if wr_f < float(self.cfg.kill_wr) or wlb < float(self.cfg.kill_wr) - 0.05:
            self._stance = "veto_loosen"
            self._veto_loosen = True
            return "tighten"

        # Healthy WR but rich fill rate → mild tighten (lock edge).
        if wr_f >= float(self.cfg.target_wr) and fph > float(self.cfg.rich_fph):
            self._stance = "exploit_tighten"
            return "tighten"

        # Starved fills with acceptable Wilson LB → loosen for learning throughput.
        if fph < float(self.cfg.starve_fph) and wlb >= float(self.cfg.kill_wr) - 0.02:
            self._stance = "explore_loosen"
            return "loosen"

        # Below target but not kill → tighten selectivity.
        if wr_f < float(self.cfg.target_wr) and fph >= float(self.cfg.starve_fph):
            self._stance = "exploit_tighten"
            return "tighten"

        self._stance = "hold"
        return None

    def step_scale(self) -> float:
        """Adaptive η shrink with regime factor."""
        base = 1.0 / (1.0 + math.sqrt(max(0, self._n_adjustments)))
        return float(base * self._regime_factor)

    def veto_loosen(self) -> bool:
        """Subordinate tuners should skip loosen when True."""
        return bool(self._veto_loosen) and bool(self.cfg.enabled)

    # ---- decision-time affinity ----
    def evaluate_pre_trade(
        self,
        *,
        side: str,
        ask: float,
        asset: str = "btc",
        lane: str = "15m",
    ) -> dict:
        """Return size_mult / soft_block from Beta side affinity (PAPER ONLY)."""
        out = {
            "enabled": bool(self.cfg.enabled),
            "size_mult": 1.0,
            "soft_block": False,
            "affinity_mean": None,
            "affinity_n": 0,
            "stance": self._stance,
            "edge_vs_ask": None,
        }
        if not self.cfg.enabled:
            return out
        sd = str(side or "").lower()
        if sd not in ("up", "down"):
            return out
        aff = self._get_aff(str(asset).lower(), str(lane).lower(), sd)
        n = aff.samples(self.cfg.beta_alpha0, self.cfg.beta_beta0)
        mean = aff.mean
        out["affinity_mean"] = round(mean, 4)
        out["affinity_n"] = n
        try:
            ask_f = float(ask)
        except (TypeError, ValueError):
            return out
        if ask_f <= 0 or ask_f >= 1:
            return out
        edge = mean - ask_f
        out["edge_vs_ask"] = round(edge, 4)

        # Soft block: enough samples and posterior mean below ask by margin.
        if n >= int(self.cfg.side_min_n) and edge < -float(self.cfg.soft_block_edge):
            out["soft_block"] = True
            out["size_mult"] = float(self.cfg.size_cut_min)
            return out

        # Scale size by edge (clipped). Positive edge → boost; negative → cut.
        if n >= 3:
            # Map edge ∈ [-0.15, 0.15] → [size_cut_min, size_boost_max]
            t = _clamp(edge / 0.10, -1.0, 1.0)
            lo = float(self.cfg.size_cut_min)
            hi = float(self.cfg.size_boost_max)
            out["size_mult"] = round(lo + (hi - lo) * (t + 1.0) / 2.0, 4)
        return out

    # ---- apply ----
    def maybe_adjust(self, engine) -> Optional[dict]:
        """Coordinated scalar nudge on engine.cfg. Returns adjustment dict or None."""
        if not self.cfg.enabled:
            return None
        if self._since_adjust < int(self.cfg.cooldown_settlements):
            return None
        roll = self._rolling()
        u = self.utility(roll)
        self._last_utility = u
        action = self._decide(roll)
        if action is None:
            return None

        scale = self.step_scale()
        cfg = engine.cfg
        before = {
            "min_edge": float(cfg.min_edge),
            "min_entry_price": float(cfg.min_entry_price),
            "exec_min_ev": float(cfg.exec_min_ev_after_slippage),
        }
        sign = 1.0 if action == "tighten" else -1.0
        cfg.min_edge = _clamp(
            before["min_edge"] + sign * float(self.cfg.step_edge) * scale,
            self.cfg.min_edge_lo, self.cfg.min_edge_hi)
        cfg.min_entry_price = _clamp(
            before["min_entry_price"] + sign * float(self.cfg.step_entry) * scale,
            self.cfg.min_entry_lo, self.cfg.min_entry_hi)
        cfg.exec_min_ev_after_slippage = _clamp(
            before["exec_min_ev"] + sign * float(self.cfg.step_ev) * scale,
            self.cfg.exec_ev_lo, self.cfg.exec_ev_hi)

        # Mirror sweet band gently via tier engine when available.
        te = getattr(engine, "tier_engine", None)
        sweet_before = None
        if te is not None and getattr(te, "cfg", None) is not None:
            sweet_before = {
                "sweet_min": float(te.cfg.sweet_min),
                "sweet_max": float(te.cfg.sweet_max),
            }
            te.cfg.sweet_min = _clamp(
                sweet_before["sweet_min"] + sign * 0.008 * scale, 0.40, 0.58)
            te.cfg.sweet_max = _clamp(
                sweet_before["sweet_max"] - sign * 0.01 * scale, 0.62, 0.90)

        after = {
            "min_edge": float(cfg.min_edge),
            "min_entry_price": float(cfg.min_entry_price),
            "exec_min_ev": float(cfg.exec_min_ev_after_slippage),
        }
        adj = {
            "action": action,
            "stance": self._stance,
            "utility": u,
            "step_scale": round(scale, 4),
            "regime_factor": self._regime_factor,
            "veto_loosen": self._veto_loosen,
            "rolling": roll,
            "before": before,
            "after": after,
            "sweet_before": sweet_before,
            "ts": time.time(),
            "method": "sawr_fill_quality_pareto",
        }
        self._adjustments.append(adj)
        if len(self._adjustments) > 40:
            self._adjustments = self._adjustments[-40:]
        self._since_adjust = 0
        self._n_adjustments += 1
        self._last_action = action
        self._last_ts = adj["ts"]
        return adj

    def report(self) -> dict:
        roll = self._rolling()
        aff_rep = {}
        for k, aff in self._affinity.items():
            aff_rep[k] = {
                "mean": round(aff.mean, 4),
                "alpha": round(aff.alpha, 3),
                "beta": round(aff.beta, 3),
                "n": aff.samples(self.cfg.beta_alpha0, self.cfg.beta_beta0),
            }
        brier = None
        if self._brier_n >= 5:
            brier = {
                "n": self._brier_n,
                "model": round(self._model_brier_sum / self._brier_n, 5),
                "market": (round(self._mkt_brier_sum / self._brier_n, 5)
                           if self._mkt_brier_sum > 0 else None),
            }
        return {
            "enabled": bool(self.cfg.enabled),
            "method": "sawr_fill_quality_pareto_v1",
            "stance": self._stance,
            "veto_loosen": self._veto_loosen,
            "utility": self.utility(roll),
            "rolling": roll,
            "last_action": self._last_action,
            "last_ts": self._last_ts,
            "n_adjustments": self._n_adjustments,
            "since_adjust": self._since_adjust,
            "cooldown_settlements": int(self.cfg.cooldown_settlements),
            "step_scale": round(self.step_scale(), 4),
            "regime_factor": self._regime_factor,
            "targets": {
                "target_wr": float(self.cfg.target_wr),
                "kill_wr": float(self.cfg.kill_wr),
                "starve_fph": float(self.cfg.starve_fph),
                "rich_fph": float(self.cfg.rich_fph),
                "wr_weight": float(self.cfg.wr_weight),
                "fill_weight": float(self.cfg.fill_weight),
                "kill_penalty": float(self.cfg.kill_penalty),
            },
            "side_affinity": aff_rep,
            "brier": brier,
            "recent_adjustments": self._adjustments[-8:],
            "note": (
                "SAWR meta-controller: Fill-Quality Pareto utility + Empirical-Bayes "
                "side affinity + adaptive step shrink. Arbitrates WR vs fill rate. PAPER ONLY."
            ),
        }

    def to_state(self) -> dict:
        return {
            "recent": list(self._recent),
            "since_adjust": self._since_adjust,
            "n_adjustments": self._n_adjustments,
            "stance": self._stance,
            "veto_loosen": self._veto_loosen,
            "last_action": self._last_action,
            "last_ts": self._last_ts,
            "adjustments": self._adjustments[-20:],
            "affinity": {
                k: {"alpha": v.alpha, "beta": v.beta} for k, v in self._affinity.items()
            },
            "model_brier_sum": self._model_brier_sum,
            "mkt_brier_sum": self._mkt_brier_sum,
            "brier_n": self._brier_n,
            "regime_factor": self._regime_factor,
        }

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self._recent.clear()
        for row in (data.get("recent") or [])[-int(self.cfg.lookback_n):]:
            if isinstance(row, dict):
                self._recent.append(row)
        self._since_adjust = int(data.get("since_adjust") or 0)
        self._n_adjustments = int(data.get("n_adjustments") or 0)
        self._stance = str(data.get("stance") or "hold")
        self._veto_loosen = bool(data.get("veto_loosen"))
        self._last_action = data.get("last_action")
        self._last_ts = data.get("last_ts")
        self._adjustments = list(data.get("adjustments") or [])[-40:]
        self._affinity = {}
        for k, v in (data.get("affinity") or {}).items():
            if isinstance(v, dict):
                self._affinity[k] = BetaAffinity(
                    alpha=float(v.get("alpha") or self.cfg.beta_alpha0),
                    beta=float(v.get("beta") or self.cfg.beta_beta0),
                )
        self._model_brier_sum = float(data.get("model_brier_sum") or 0.0)
        self._mkt_brier_sum = float(data.get("mkt_brier_sum") or 0.0)
        self._brier_n = int(data.get("brier_n") or 0)
        self._regime_factor = float(data.get("regime_factor") or 1.0)
