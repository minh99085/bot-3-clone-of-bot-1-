"""Directional Tier Engine — the "very smart" regime-aware directional brain (PAPER ONLY).

Trades the Polymarket crypto up/down digital as a *sequential, regime-conditioned* decision, not a
direction prediction. Conviction escalates through the window as information arrives; capital is
sized by tier; the near-deterministic late window is sniped.

Signal ladder (TradingView RSI per asset):
  5m               fast intra-window momentum + reversal/jump early-warning
  15m, 30m, 45m    momentum voters (multi-timeframe agreement)
  60m (1h)         window directional bias (the prior)
  240m (4h)        trend-vs-chop REGIME  — flips the sign of momentum likelihoods
  1440m (1d)       macro trend filter

Posterior (log-odds, regime-conditioned):
  logit(P_up) = logit(fair_displacement)                     # digital anchor; -> 0/1 late window
              + logit(prior_HTF)-logit(0.5)                  # 1h/4h/1d regime bias
              + Σ_mtf  LR_mtf(dir, regime) x freshness        # 3/4/5/15/30/45m, graded per regime

Tiers (by window state x conviction x edge):
  SNIPE   last <= 8m, |z| >= z_min, book stale, jump-veto clear   -> max size, ~95-99% win
  STRIKE  12-35m, MTF aligned + HTF agrees, edge >= strike_min     -> large
  HARVEST HTF bias + 1 TF confirm + sweet price, edge >= harv_min  -> medium
  PROBE   early, HTF bias only                                      -> $5 learning
  WAIT    else / chop-no-displacement / jump risk / edge < 0        -> $0

Real-money discipline (paper): fractional-Kelly on fat-tail variance, per-tier + depth caps,
concurrent-exposure cap, daily-loss halt, rising-conviction gate. execution_gate stays the sole
fill authority — this engine only proposes (side, size). PAPER ONLY.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time as _time
import copy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from engine.pulse.fair_value import digital_p_up
from engine.pulse.prism.belief import freshness, logit, sigmoid
from engine.pulse.selectivity import _wilson_upper

logger = logging.getLogger("pulse.tier_engine")

_STORE = "tier_lr_table.json"

# TF roles (minute-string keys as they arrive from TradingView).
MTF_TFS = ("3", "4", "5", "15", "30", "45")      # momentum voters (3/4 wired 2026-07-08)
HTF_TFS = ("60", "240", "1440")        # regime / prior (1h, 4h, 1d)

# Per-TF freshness half-life (seconds) — HTF decays slowly, fast TF quickly.
_HALF_LIFE = {"3": 360.0, "4": 420.0, "5": 450.0, "15": 1200.0, "30": 2400.0, "45": 3600.0,
              "60": 5400.0, "240": 21600.0, "1440": 86400.0}


class Tier(str, Enum):
    SNIPE = "snipe"
    STRIKE = "strike"
    HARVEST = "harvest"
    PROBE = "probe"
    WAIT = "wait"


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CHOP = "chop"
    NEUTRAL = "neutral"


def _dir_sign(d) -> int:
    s = str(d or "").strip().upper()
    if s in ("UP", "LONG", "BUY", "BULL"):
        return 1
    if s in ("DOWN", "SHORT", "SELL", "BEAR"):
        return -1
    return 0


@dataclass
class TierConfig:
    bankroll_usd: float = 2000.0
    snipe_max_usd: float = 200.0
    strike_max_usd: float = 120.0
    harvest_max_usd: float = 25.0
    probe_usd: float = 5.0
    snipe_z_min: float = 2.3
    snipe_ttc_s: float = 480.0            # last 8 min
    strike_edge_min: float = 0.04
    harvest_edge_min: float = 0.025
    kelly_fraction: float = 0.25          # fractional Kelly on fat-tail variance
    depth_cap_frac: float = 0.25
    daily_loss_halt_pct: float = 0.10
    max_concurrent: int = 6
    slippage_buffer: float = 0.01
    sweet_min: float = 0.47
    sweet_max: float = 0.55
    min_seconds_since_open: float = 180.0

    @classmethod
    def from_env(cls) -> "TierConfig":
        def _f(k, d):
            try:
                return float(os.getenv(k, str(d)))
            except (TypeError, ValueError):
                return d
        return cls(
            bankroll_usd=_f("PULSE_TIER_BANKROLL_USD", 2000.0),
            snipe_max_usd=_f("PULSE_TIER_SNIPE_MAX_USD", 200.0),
            strike_max_usd=_f("PULSE_TIER_STRIKE_MAX_USD", 120.0),
            harvest_max_usd=_f("PULSE_TIER_HARVEST_MAX_USD", 25.0),
            probe_usd=_f("PULSE_TIER_PROBE_USD", 5.0),
            snipe_z_min=_f("PULSE_TIER_SNIPE_Z_MIN", 2.3),
            strike_edge_min=_f("PULSE_TIER_STRIKE_EDGE_MIN", 0.04),
            harvest_edge_min=_f("PULSE_TIER_HARVEST_EDGE_MIN", 0.025),
            kelly_fraction=_f("PULSE_TIER_KELLY_FRACTION", 0.25),
            daily_loss_halt_pct=_f("PULSE_TIER_DAILY_LOSS_HALT_PCT", 0.10),
            max_concurrent=int(_f("PULSE_TIER_MAX_CONCURRENT", 6)),
            # High-WR Mode: sweet band + watching floor must match favorites (env-driven).
            sweet_min=_f("PULSE_TIER_SWEET_MIN", 0.47),
            sweet_max=_f("PULSE_TIER_SWEET_MAX", 0.55),
            min_seconds_since_open=_f("PULSE_TIER_MIN_SECONDS_SINCE_OPEN", 180.0),
            slippage_buffer=_f("PULSE_TIER_SLIPPAGE_BUFFER", 0.01),
        )


@dataclass
class TierDecision:
    tier: Tier
    side: Optional[str]          # "up" | "down" | None
    p_up: float
    edge: float                  # chosen-side edge = P(chosen) - ask
    conviction: float            # |2*P_up - 1|
    size_usd: float
    regime: Regime
    reason: str
    z: float = 0.0
    breakdown: dict = field(default_factory=dict)

    @property
    def trade(self) -> bool:
        return self.tier != Tier.WAIT and self.side is not None and self.size_usd > 0

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value, "side": self.side, "p_up": round(self.p_up, 5),
            "edge": round(self.edge, 5), "conviction": round(self.conviction, 4),
            "size_usd": round(self.size_usd, 2), "regime": self.regime.value,
            "z": round(self.z, 3), "reason": self.reason, "breakdown": self.breakdown,
        }


class RegimeLikelihoods:
    """Per-(timeframe, regime) likelihood ratios for a momentum signal aligned with the trade side.

    Trend regime: aligned momentum CONFIRMS (LR>1). Chop regime: aligned momentum FADES (LR<1) —
    this is the fix for the old negative-alpha "buy momentum in chop" behavior. Graded nightly from
    settled outcomes (Wilson-floored) and persisted to disk.
    """

    DEFAULTS = {
        regime.value: {tf: 1.0 for tf in ("3", "4", "5", "15", "30", "45")}
        for regime in Regime
    }

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir) if data_dir else None
        self.table = {r: dict(v) for r, v in self.DEFAULTS.items()}
        # (regime|tf) -> {"aligned_wins","aligned_n"} for nightly grading
        self.grades: dict = {}
        if self.data_dir is not None:
            self.load()

    @property
    def path(self) -> Optional[Path]:
        return (self.data_dir / _STORE) if self.data_dir is not None else None

    def lr(self, tf: str, regime: Regime, side_sign: int, tf_sign: int) -> float:
        """LR for this TF's momentum given the proposed side. Aligned (tf_sign==side_sign) uses the
        table LR; opposed uses its reciprocal; neutral tf -> 1.0 (no evidence)."""
        if tf_sign == 0 or side_sign == 0:
            return 1.0
        # Disabled until a chronological lane-specific estimator exists.  The former table used
        # win odds from aligned trades as if they were a likelihood ratio and inferred the opposed
        # value by reciprocal, which is not statistically valid.
        return 1.0

    def record(self, regime: Regime, tf: str, aligned: bool, won: bool) -> None:
        key = "%s|%s" % (regime.value, tf)
        g = self.grades.setdefault(key, {"aligned_wins": 0, "aligned_n": 0})
        if aligned:
            g["aligned_n"] += 1
            if won:
                g["aligned_wins"] += 1

    def recalibrate(self, *, min_n: int = 30, z: float = 1.64) -> None:
        """Retained as a compatibility no-op until lane-specific chronological LRs exist."""
        self.table = {r: dict(v) for r, v in self.DEFAULTS.items()}

    def load(self) -> None:
        p = self.path
        if p is None or not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        # Legacy learned tables used invalid win-odds-as-LR math; never restore them.
        self.table = {r: dict(v) for r, v in self.DEFAULTS.items()}
        self.grades = {k: dict(v) for k, v in (data.get("grades") or {}).items()}

    def save(self) -> None:
        p = self.path
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"schema": "tier_lr/1.0", "table": self.table,
                                     "grades": self.grades}, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def classify_regime(htf: dict) -> Regime:
    """Regime from HTF TV reads. htf maps tf -> (sign, strength). 4h dominant, 1h confirm, 1d filter."""
    def sd(tf):
        s = htf.get(tf)
        return (0, 0.0) if not s else (int(s[0]), float(s[1] or 0.0))
    d4, s4 = sd("240")
    d1, s1 = sd("60")
    dd, sd_ = sd("1440")
    if d4 == 0:
        # no 4h read -> fall back to 1h
        if d1 > 0 and s1 >= 0.5:
            return Regime.TREND_UP
        if d1 < 0 and s1 >= 0.5:
            return Regime.TREND_DOWN
        return Regime.NEUTRAL
    strong = s4 >= 0.5
    aligned_1h = (d1 == d4) or d1 == 0
    macro_conflict = (dd != 0 and dd == -d4 and sd_ >= 0.6)
    if strong and aligned_1h and not macro_conflict:
        return Regime.TREND_UP if d4 > 0 else Regime.TREND_DOWN
    if not strong or (d1 != 0 and d1 == -d4):
        return Regime.CHOP
    return Regime.NEUTRAL


def _prior_p_up(regime: Regime, htf: dict) -> float:
    return 0.50


class DirectionalTierEngine:
    """Owns regime + LR grading + per-window conviction memory + tier sizing. PAPER ONLY."""

    def __init__(self, cfg: Optional[TierConfig] = None, data_dir: Optional[Path] = None):
        self.cfg = cfg or TierConfig()
        self.lrs = RegimeLikelihoods(data_dir)
        self._last_conviction: dict = {}     # window_key -> last conviction (rising-gate)
        self._day_key: Optional[int] = None
        self._daily_pnl: float = 0.0
        self.counts: dict = {t.value: 0 for t in Tier}
        self._last_decision: dict = {}       # immutable ENTRY decision -> grade at settle

    # ---- daily loss halt ----
    def record_pnl(self, pnl_usd: float, now: Optional[float] = None) -> None:
        day = int((now if now is not None else _time.time()) // 86400)
        if day != self._day_key:
            self._day_key, self._daily_pnl = day, 0.0
        self._daily_pnl += float(pnl_usd or 0.0)

    def _halted(self) -> bool:
        return (-min(0.0, self._daily_pnl)) >= self.cfg.daily_loss_halt_pct * self.cfg.bankroll_usd

    # ---- core decision ----
    def evaluate(self, *, window_key: str, sso: float, ttc_s: float, s_now: float, s_open: float,
                 sigma_per_sec: float, ask_up: Optional[float], ask_down: Optional[float],
                 tv_by_tf: dict, now: float, ask_depth_up: Optional[float] = None,
                 ask_depth_down: Optional[float] = None, open_corr: float = 0.0,
                 jump_risk: bool = False, down_only: bool = False,
                 window_seconds: float = 3600.0,
                 overlay: Optional[dict] = None) -> TierDecision:
        """tv_by_tf: {tf: {"direction","strength","ts"}} for THIS window's asset.

        ``window_seconds`` scales SSO/TTC tier bands (1h defaults → 15m via scale=ws/3600).
        ``overlay`` optionally overrides sweet/edge/SSO floors for a lane-local learner.
        """
        # --- parse TV ladder into signed, fresh reads ---
        def read(tf):
            snap = (tv_by_tf or {}).get(tf) or {}
            sign = _dir_sign(snap.get("direction"))
            strength = float(snap.get("strength") or 0.5)
            ts = snap.get("ts")
            fr = freshness((now - float(ts)) if ts is not None else 1e9, _HALF_LIFE.get(tf))
            return sign, strength, fr
        htf = {tf: (read(tf)[0], read(tf)[1] * read(tf)[2]) for tf in HTF_TFS}
        regime = classify_regime(htf)

        # --- digital anchor (dominates late window: fair -> 0/1 as ttc -> 0) ---
        fair_disp = digital_p_up(s_now, s_open, sigma_per_sec, ttc_s)
        if fair_disp is None:
            fair_disp = 0.5
        z = 0.0
        if sigma_per_sec and ttc_s > 0 and s_now and s_open and s_now > 0 and s_open > 0:
            z = math.log(s_now / s_open) / (sigma_per_sec * math.sqrt(ttc_s))

        # --- choose the side the evidence favors (displacement + prior), then score its posterior ---
        prior = _prior_p_up(regime, htf)
        lo = logit(fair_disp) + (logit(prior) - logit(0.5))
        side_pre = "up" if lo >= 0 else "down"
        side_sign = 1 if side_pre == "up" else -1
        mtf_terms = {}
        for tf in MTF_TFS:
            sgn, strg, fr = read(tf)
            lr = self.lrs.lr(tf, regime, side_sign, sgn)
            shift = math.log(lr) * (strg * fr)
            lo += shift
            mtf_terms[tf] = {"sign": sgn, "fresh": round(fr, 3), "lr": round(lr, 3),
                             "shift": round(shift, 4)}
        p_up = sigmoid(lo)

        # edges per side
        e_up = (p_up - float(ask_up)) if ask_up is not None else None
        e_dn = ((1.0 - p_up) - float(ask_down)) if ask_down is not None else None
        if down_only:
            side, edge, ask, depth = "down", (e_dn if e_dn is not None else -1.0), ask_down, ask_depth_down
        else:
            cand = [(s, e, a, d) for s, e, a, d in
                    (("up", e_up, ask_up, ask_depth_up), ("down", e_dn, ask_down, ask_depth_down))
                    if e is not None]
            if cand:
                side, edge, ask, depth = max(cand, key=lambda t: t[1])
            else:
                side, edge, ask, depth = None, -1.0, None, None
        p_chosen = p_up if side == "up" else (1.0 - p_up)
        conviction = abs(2.0 * p_up - 1.0)

        # rising-conviction gate
        prev = self._last_conviction.get(window_key, 0.0)
        rising = conviction >= prev - 1e-6
        self._last_conviction[window_key] = conviction

        dec = self._assign(
            window_key, regime, side, edge, conviction, p_up, p_chosen, z, ask, depth,
            ttc_s, sso, rising, jump_risk, mtf_terms, prior, fair_disp, open_corr,
            window_seconds=float(window_seconds or 3600.0), overlay=overlay)
        self.counts[dec.tier.value] = self.counts.get(dec.tier.value, 0) + 1
        return dec

    def record_entry(self, window_key: str, decision: TierDecision) -> None:
        """Freeze the exact decision that produced a fill; later ticks must not rewrite it."""
        if decision is not None and decision.trade:
            self._last_decision[str(window_key)] = copy.deepcopy(decision)

    def _assign(self, window_key, regime, side, edge, conviction, p_up, p_chosen, z, ask, depth,
                ttc_s, sso, rising, jump_risk, mtf_terms, prior, fair_disp, open_corr,
                window_seconds: float = 3600.0, overlay: Optional[dict] = None) -> TierDecision:
        c = self.cfg
        ov = overlay or {}
        # Scale 1h-calibrated SSO bands to this window (15m → scale 0.25).
        scale = max(0.15, min(1.0, float(window_seconds) / 3600.0))
        watch_floor = float(ov.get("min_sso", c.min_seconds_since_open))
        if "min_sso" not in ov:
            # Shared hourly floor (e.g. 300s) would starve 15m — scale it.
            watch_floor = min(watch_floor, max(30.0, float(c.min_seconds_since_open) * scale))
        sweet_min = float(ov.get("sweet_min", c.sweet_min))
        sweet_max = float(ov.get("sweet_max", c.sweet_max))
        strike_edge = float(ov.get("strike_edge_min", c.strike_edge_min))
        harvest_edge = float(ov.get("harvest_edge_min", c.harvest_edge_min))
        probe_enabled = bool(ov.get("probe_enabled", True))
        snipe_ttc = float(ov.get("snipe_ttc_s", c.snipe_ttc_s))
        if "snipe_ttc_s" not in ov and scale < 0.9:
            # 15m: snipe in last ~2–3 min (not last 8m of a 1h window).
            snipe_ttc = min(snipe_ttc, max(90.0, 480.0 * scale))
        strike_sso_min = float(ov.get("strike_sso_min", 720.0 * scale))
        probe_sso_max = float(ov.get("probe_sso_max", 720.0 * scale))

        bd = {"regime": regime.value, "prior": round(prior, 3), "fair_disp": round(fair_disp, 4),
              "mtf": mtf_terms, "rising": rising, "window_seconds": round(float(window_seconds), 1),
              "scale": round(scale, 3)}

        def mk(tier, reason, size):
            return TierDecision(tier=tier, side=side, p_up=p_up, edge=edge, conviction=conviction,
                                size_usd=size, regime=regime, reason=reason, z=z, breakdown=bd)

        if side is None or ask is None:
            return mk(Tier.WAIT, "no_tradeable_side", 0.0)
        if self._halted():
            return mk(Tier.WAIT, "daily_loss_halt", 0.0)
        if sso < watch_floor:
            return mk(Tier.WAIT, "watching_floor", 0.0)
        if edge < 0:
            return mk(Tier.WAIT, "negative_edge", 0.0)

        # SNIPE — last minutes, decisive displacement, stale book, jump-veto clear
        if ttc_s <= snipe_ttc and abs(z) >= c.snipe_z_min and edge >= c.slippage_buffer:
            if jump_risk:
                return mk(Tier.WAIT, "snipe_jump_veto", 0.0)
            return mk(Tier.SNIPE, "nowcast_decisive", self._size(Tier.SNIPE, edge, p_chosen, ask, depth, open_corr))

        # STRIKE — mid window, MTF aligned + regime agrees, strong edge, conviction rising
        mtf_aligned = all(mtf_terms.get(tf, {}).get("sign", 0) ==
                          (1 if side == "up" else -1) for tf in ("15", "30")) \
            and mtf_terms.get("5", {}).get("sign", 0) == (1 if side == "up" else -1)
        regime_ok = (regime == Regime.TREND_UP and side == "up") or \
                    (regime == Regime.TREND_DOWN and side == "down") or regime == Regime.NEUTRAL
        if (sso >= strike_sso_min and edge >= strike_edge and conviction >= 0.30
                and mtf_aligned and regime_ok and rising):
            return mk(Tier.STRIKE, "mtf_aligned_regime", self._size(Tier.STRIKE, edge, p_chosen, ask, depth, open_corr))

        # HARVEST — regime bias + one confirm + sweet price + modest edge
        one_confirm = any(mtf_terms.get(tf, {}).get("sign", 0) == (1 if side == "up" else -1)
                          for tf in MTF_TFS)
        sweet = (sweet_min <= float(ask) <= sweet_max)
        if (edge >= harvest_edge and one_confirm and sweet and regime != Regime.CHOP):
            return mk(Tier.HARVEST, "regime_bias_sweet", self._size(Tier.HARVEST, edge, p_chosen, ask, depth, open_corr))

        # PROBE — early learning, tiny flat size, only with some HTF bias
        if (probe_enabled and sso < probe_sso_max
                and regime in (Regime.TREND_UP, Regime.TREND_DOWN) and edge >= 0.01):
            return mk(Tier.PROBE, "early_probe", c.probe_usd)

        return mk(Tier.WAIT, "below_tier_thresholds", 0.0)

    def _size(self, tier: Tier, edge: float, p_win: float, ask: float,
              depth: Optional[float], open_corr: float) -> float:
        c = self.cfg
        cap = {Tier.SNIPE: c.snipe_max_usd, Tier.STRIKE: c.strike_max_usd,
               Tier.HARVEST: c.harvest_max_usd}.get(tier, c.probe_usd)
        # fractional Kelly on a 0/1 payout bought at ask: f* = (p - ask)/(1 - ask)
        if ask is not None and 0.0 < float(ask) < 1.0:
            kelly_f = max(0.0, (float(p_win) - float(ask)) / (1.0 - float(ask)))
        else:
            kelly_f = 0.0
        raw = c.bankroll_usd * c.kelly_fraction * kelly_f
        size = min(raw, cap)
        if depth is not None and depth > 0:
            size = min(size, c.depth_cap_frac * float(depth))
        if open_corr > 0:
            size *= max(0.0, 1.0 - float(open_corr))
        return round(max(0.0, size), 2)

    # ---- grading at settle ----
    def record_settled(self, window_key: str, *, won: bool, pnl_usd: float, now: float) -> None:
        self.record_pnl(pnl_usd, now)
        dec = self._last_decision.pop(window_key, None)
        self._last_conviction.pop(window_key, None)
        if dec is None or dec.side is None:
            return
        side_sign = 1 if dec.side == "up" else -1
        for tf, t in (dec.breakdown.get("mtf") or {}).items():
            aligned = int(t.get("sign", 0)) == side_sign
            self.lrs.record(dec.regime, tf, aligned=aligned, won=won)
        self.lrs.save()

    def recalibrate(self) -> None:
        self.lrs.recalibrate()
        self.lrs.save()

    def to_report(self) -> dict:
        total = sum(self.counts.values())
        return {
            "enabled": True, "bankroll_usd": self.cfg.bankroll_usd,
            "daily_pnl": round(self._daily_pnl, 2), "halted": self._halted(),
            "tier_counts": dict(self.counts), "total_decisions": total,
            "lr_table": self.lrs.table,
            "open_windows_tracked": len(self._last_decision),
        }
