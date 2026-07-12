"""Dynamic pre-trade analysis — synthesize ALL data at hand before a directional fill (PAPER ONLY).

The quant stack already computes many observe-only features (research, edge_signal, council views,
TV trend, book microstructure) but they were fragmented: council traded on static margin/agreement
thresholds without a unified read on whether the *whole* picture supported entry NOW.

This module is the "quant researcher at the desk" step:
  1. Score independent evidence components (edge, alignment, CEX, book, timing, microstructure).
  2. Blend into one readiness score + plain-language recommendation.
  3. RESTRICT-ONLY effects: raise council conviction bar and scale size when readiness is low;
     hard-block only clearly weak setups (with a small exploration carve-out for learning).
  4. Grade every analysis bucket vs realized outcomes so the weights self-correct over time.

Invariants:
  * Never bypasses evaluate_execution() — execution gate stays authoritative.
  * Never re-enables TradingView trade gates — TV feeds components only.
  * Can only make the bot MORE selective (or smaller), never force a trade.
"""

from __future__ import annotations

import math
import random
from typing import Optional

from engine.pulse.hourly_entry_timing import hourly_entry_bucket, is_hourly_window
from engine.pulse.selectivity import (
    benjamini_hochberg,
    breakeven_win_rate,
    profit_factor_from_stat,
    _binom_cdf_le,
    _wilson_upper,
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _dispersion_score(values: list) -> Optional[float]:
    """1 - normalized stdev of member p_up views (1.0 = perfect agreement)."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / len(vals)
    # max meaningful spread on [0,1] is ~0.5 when views split 0/1
    return _clamp01(1.0 - math.sqrt(var) / 0.5)


def _book_quality_score(spread: Optional[float], ask_depth_usd: Optional[float],
                          *, max_spread: float = 0.06, min_depth: float = 200.0) -> Optional[float]:
    if spread is None:
        return None
    spread_s = _clamp01(1.0 - float(spread) / max(max_spread, 1e-6))
    depth_s = (_clamp01(float(ask_depth_usd or 0.0) / min_depth)
               if ask_depth_usd is not None else 0.5)
    return round(0.55 * spread_s + 0.45 * depth_s, 4)


def _timing_fit_score(*, window_seconds: int, seconds_since_open: float, ttc_s: float,
                      hourly_min_minutes: float) -> float:
    """Window-aware timing: 1h ramps through intrahour ladder; short windows favor mid-TTC."""
    ws = int(window_seconds or 300)
    sso = max(0.0, float(seconds_since_open))
    if is_hourly_window(ws):
        # Soft ramp to hourly_min_minutes (default 12m): TV 15/30/45/55m ladder needs time.
        ramp_end = max(60.0, float(hourly_min_minutes) * 60.0)
        early = _clamp01(sso / ramp_end)
        # slight bonus for not sniping the last 90s (execution risk)
        late_pen = _clamp01(float(ttc_s) / 90.0) if ttc_s < 90 else 1.0
        return round(0.7 * early + 0.3 * late_pen, 4)
    # 5m/15m: sweet spot ~120-240s TTC (proven cohort band on baseline path)
    ttc = float(ttc_s)
    if ttc >= 240:
        return 0.85
    if ttc >= 180:
        return 1.0
    if ttc >= 120:
        return 0.75
    if ttc >= 60:
        return 0.45
    return 0.25


def _cex_confirm_score(edge_snap: Optional[dict], proposed_side: Optional[str]) -> Optional[float]:
    if not edge_snap or not proposed_side:
        return None
    mom = (edge_snap.get("cex_momentum") or {})
    direction = mom.get("basket_direction")
    agree = mom.get("exchange_agreement")
    if direction not in ("up", "down"):
        return 0.5 if agree is None else _clamp01(0.35 + 0.65 * float(agree))
    aligned = (direction == proposed_side)
    base = float(agree) if agree is not None else 0.66
    return _clamp01(base if aligned else (1.0 - base))


def _edge_executable_score(*, fair_p_up: Optional[float], poly_yes: Optional[float],
                             proposed_side: Optional[str], up_ask: Optional[float],
                             down_ask: Optional[float], min_edge: float) -> Optional[float]:
    """How strong is the executable edge for the proposed (or consensus-favorite) side?"""
    if fair_p_up is None:
        return None
    fp = float(fair_p_up)
    if proposed_side == "up" and up_ask is not None:
        ev = fp - float(up_ask)
    elif proposed_side == "down" and down_ask is not None:
        ev = (1.0 - fp) - float(down_ask)
    elif poly_yes is not None:
        ev = abs(fp - float(poly_yes))
    else:
        ev = abs(fp - 0.5)
    if ev <= 0:
        return 0.0
    # map min_edge..0.15 -> 0.35..1.0
    ref = max(float(min_edge), 0.15)
    return _clamp01(0.35 + 0.65 * min(1.0, ev / ref))


def analyze_pre_trade(
    *,
    fair_p_up: Optional[float],
    poly_yes: Optional[float],
    council_views: Optional[dict] = None,
    proposed_side: Optional[str] = None,
    proposed_p_up: Optional[float] = None,
    edge_snap: Optional[dict] = None,
    features: Optional[dict] = None,
    ttc_s: float,
    window_seconds: int,
    seconds_since_open: float,
    spread: Optional[float] = None,
    ask_depth_usd: Optional[float] = None,
    price_fresh: bool = True,
    vol_trusted: bool = True,
    up_ask: Optional[float] = None,
    down_ask: Optional[float] = None,
    min_edge: float = 0.004,
    hourly_min_minutes: float = 12.0,
    component_weights: Optional[dict] = None,
    tv_2h_review: Optional[dict] = None,
    tv_per_tf_views: Optional[dict] = None,
) -> dict:
    """Synthesize all inputs into a scored pre-trade analysis dict (deterministic, no LLM)."""
    views = dict(council_views or {})
    view_vals = [views.get(k) for k in ("quant", "grok", "claude", "tv_mtf") if views.get(k) is not None]
    w = dict(component_weights or {})
    w_def = {
        "edge_executable": 0.22,
        "member_alignment": 0.18,
        "cex_confirmation": 0.14,
        "book_quality": 0.12,
        "timing_fit": 0.16,
        "microstructure": 0.10,
        "oracle_health": 0.08,
        "tv_ladder_alignment": 0.12,
    }
    for k, v in w_def.items():
        w.setdefault(k, v)

    components = {}
    components["edge_executable"] = _edge_executable_score(
        fair_p_up=fair_p_up, poly_yes=poly_yes, proposed_side=proposed_side,
        up_ask=up_ask, down_ask=down_ask, min_edge=min_edge)
    components["member_alignment"] = _dispersion_score(view_vals)
    components["cex_confirmation"] = _cex_confirm_score(edge_snap, proposed_side)
    components["book_quality"] = _book_quality_score(spread, ask_depth_usd)
    components["timing_fit"] = _timing_fit_score(
        window_seconds=window_seconds, seconds_since_open=seconds_since_open,
        ttc_s=ttc_s, hourly_min_minutes=hourly_min_minutes)
    micro = None
    if edge_snap is not None:
        micro = edge_snap.get("pulse_edge_score")
        if micro is None:
            micro = edge_snap.get("edge_quality_score")
    if micro is not None:
        components["microstructure"] = _clamp01(float(micro))
    components["oracle_health"] = (1.0 if (price_fresh and vol_trusted) else 0.2)
    tv_segment_scores = None
    if tv_2h_review:
        from engine.pulse.tv_2h_review import (
            segment_alignment_scores,
            tv_2h_alignment_score,
        )
        align = tv_2h_alignment_score(tv_2h_review, proposed_side)
        if align is not None:
            components["tv_2h_alignment"] = align
            w["tv_2h_alignment"] = float(w.get("tv_2h_alignment") or 0.10)
        tv_segment_scores = segment_alignment_scores(tv_2h_review, proposed_side)

    if tv_per_tf_views:
        from engine.pulse.tv_2h_review import tv_ladder_alignment
        ladder = tv_ladder_alignment(tv_per_tf_views, proposed_side)
        if ladder is not None:
            components["tv_ladder_alignment"] = ladder
            w["tv_ladder_alignment"] = float(w.get("tv_ladder_alignment") or 0.12)

    # weighted mean over present components
    num, den = 0.0, 0.0
    for name, val in components.items():
        if val is None:
            continue
        wt = float(w.get(name, 0.0))
        if wt <= 0:
            continue
        num += wt * float(val)
        den += wt
    score = round(num / den, 4) if den > 0 else 0.5

    # recommendation tiers
    if score >= 0.62:
        rec = "trade"
    elif score >= 0.48:
        rec = "cautious"
    else:
        rec = "wait"

    bucket = "na"
    if is_hourly_window(window_seconds):
        bucket = hourly_entry_bucket(seconds_since_open, window_seconds=window_seconds)

    narrative_parts = []
    if components.get("edge_executable") is not None:
        narrative_parts.append("edge=%.2f" % components["edge_executable"])
    if components.get("member_alignment") is not None:
        narrative_parts.append("align=%.2f" % components["member_alignment"])
    if components.get("timing_fit") is not None:
        narrative_parts.append("timing=%.2f" % components["timing_fit"])
    if components.get("cex_confirmation") is not None:
        narrative_parts.append("cex=%.2f" % components["cex_confirmation"])
    if components.get("tv_2h_alignment") is not None:
        narrative_parts.append("tv2h=%.2f" % components["tv_2h_alignment"])
    if components.get("tv_ladder_alignment") is not None:
        narrative_parts.append("tv_ladder=%.2f" % components["tv_ladder_alignment"])
    if tv_segment_scores:
        act = (tv_segment_scores.get("actionable_trend") or {}).get("score")
        op = (tv_segment_scores.get("open_regime") or {}).get("score")
        if act is not None:
            narrative_parts.append("tv_inband=%.2f" % act)
        if op is not None:
            narrative_parts.append("tv_early=%.2f" % op)

    out = {
        "score": score,
        "recommendation": rec,
        "components": {k: (round(v, 4) if v is not None else None) for k, v in components.items()},
        "weights": dict(w),
        "hourly_entry_bucket": bucket,
        "seconds_since_open": round(float(seconds_since_open), 1),
        "proposed_side": proposed_side,
        "proposed_p_up": (round(float(proposed_p_up), 4) if proposed_p_up is not None else None),
        "n_council_views": len(view_vals),
        "summary": ("readiness %.2f (%s): %s" % (score, rec, ", ".join(narrative_parts))),
        "paper_only": True,
        "restrict_only": True,
    }
    if tv_segment_scores is not None:
        out["tv_segment_scores"] = tv_segment_scores
    return out


def dynamic_council_thresholds(
    analysis: dict,
    *,
    base_margin: float,
    base_agreement: float,
    margin_boost_max: float,
    agreement_boost_max: float,
) -> dict:
    """Raise council conviction bar when readiness is low (never lowers below base)."""
    score = float(analysis.get("score") or 0.5)
    gap = _clamp01(1.0 - score)
    eff_margin = round(float(base_margin) + gap * float(margin_boost_max), 4)
    eff_agreement = round(float(base_agreement) + gap * float(agreement_boost_max), 4)
    return {
        "base_margin": float(base_margin),
        "base_agreement": float(base_agreement),
        "effective_margin": eff_margin,
        "effective_agreement": eff_agreement,
        "margin_boost": round(eff_margin - float(base_margin), 4),
        "agreement_boost": round(eff_agreement - float(base_agreement), 4),
        "readiness_score": score,
    }


class PreTradeEvidence:
    """Grade pre-trade readiness buckets vs settled outcomes (self-learning)."""

    def __init__(self):
        self.buckets: dict = {}

    @staticmethod
    def _stat() -> dict:
        return {"n": 0, "wins": 0, "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0}

    def record(self, bucket: str, *, won: bool, pnl: float) -> None:
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


def readiness_bucket(score: Optional[float]) -> str:
    if score is None:
        return "na"
    s = float(score)
    if s < 0.40:
        return "<0.40"
    if s < 0.48:
        return "0.40-0.48"
    if s < 0.62:
        return "0.48-0.62"
    return ">=0.62"


class PreTradeGate:
    """Restrict-only gate on weak pre-trade readiness (with exploration carve-out)."""

    def __init__(self, *, enabled: bool = True, min_score: float = 0.45,
                 exploration_rate: float = 0.06, min_size_scale: float = 0.35,
                 min_samples: int = 25, min_profit_factor: float = 0.85,
                 fdr_q: float = 0.10, confidence_z: float = 1.64, seed: Optional[int] = None):
        self.enabled = bool(enabled)
        self.min_score = float(min_score)
        self.exploration_rate = max(0.0, min(0.12, float(exploration_rate)))
        self.min_size_scale = max(0.1, min(1.0, float(min_size_scale)))
        self.min_samples = int(min_samples)
        self.min_profit_factor = float(min_profit_factor)
        self.fdr_q = float(fdr_q)
        self.confidence_z = float(confidence_z)
        self.accepted = 0
        self.rejected = 0
        self.explored = 0
        self.reject_reasons: dict = {}
        self._rng = random.Random(seed)

    def size_scale(self, analysis: dict) -> float:
        """Scale position size by readiness (never upsize above 1.0)."""
        score = float(analysis.get("score") or 0.5)
        rec = str(analysis.get("recommendation") or "")
        base = score if rec != "wait" else score * 0.85
        return round(max(self.min_size_scale, min(1.0, base)), 4)

    def _assess_bucket(self, st: dict) -> dict:
        n = int(st["n"])
        wr = float(st["win_rate"])
        wins = int(round(wr * n))
        be = breakeven_win_rate(st["avg_win"], st["avg_loss"])
        upper = _wilson_upper(wins, n, self.confidence_z)
        pf = profit_factor_from_stat(st)
        pf_ok = (pf is not None and pf < self.min_profit_factor)
        confidently_losing = (st["pnl_usd"] < 0) and (upper < be) and pf_ok
        p_below = _binom_cdf_le(wins, n, be) if n > 0 else 1.0
        return {
            "confidently_losing": confidently_losing,
            "p_value_vs_breakeven": round(p_below, 6),
            "profit_factor": pf,
        }

    def _bad_buckets(self, evidence: PreTradeEvidence) -> set:
        rows, keys = [], []
        for b in evidence.buckets:
            st = evidence.stat(b)
            if not st or st["n"] < self.min_samples:
                continue
            a = self._assess_bucket(st)
            if not a.get("confidently_losing"):
                continue
            rows.append(a)
            keys.append(str(b))
        if not rows:
            return set()
        flags = benjamini_hochberg([r["p_value_vs_breakeven"] for r in rows], q=self.fdr_q)
        return {k for k, ok in zip(keys, flags) if ok}

    def evaluate(self, analysis: dict, evidence: Optional[PreTradeEvidence] = None) -> dict:
        if not self.enabled:
            self.accepted += 1
            return {"decision": "accept", "reasons": ["gate_disabled"],
                    "size_scale": 1.0, "exploration": False}
        score = float(analysis.get("score") or 0.0)
        rec = str(analysis.get("recommendation") or "wait")
        bucket = readiness_bucket(score)
        bad = evidence is not None and bucket in self._bad_buckets(evidence)
        size_scale = self.size_scale(analysis)

        if bad:
            if self.exploration_rate > 0 and self._rng.random() < self.exploration_rate:
                self.explored += 1
                return {"decision": "explore", "reasons": [f"bad_readiness_bucket:{bucket}"],
                        "size_scale": size_scale, "exploration": True, "bucket": bucket}
            self.rejected += 1
            r = f"bad_readiness_bucket:{bucket}"
            self.reject_reasons[r] = self.reject_reasons.get(r, 0) + 1
            return {"decision": "reject", "reasons": [r], "size_scale": 0.0,
                    "exploration": False, "bucket": bucket}

        if rec == "wait" and score < self.min_score:
            if self.exploration_rate > 0 and self._rng.random() < self.exploration_rate:
                self.explored += 1
                return {"decision": "explore", "reasons": ["pre_trade_low_readiness"],
                        "size_scale": size_scale, "exploration": True, "bucket": bucket}
            self.rejected += 1
            self.reject_reasons["pre_trade_low_readiness"] = (
                self.reject_reasons.get("pre_trade_low_readiness", 0) + 1)
            return {"decision": "reject", "reasons": ["pre_trade_low_readiness"],
                    "size_scale": 0.0, "exploration": False, "bucket": bucket}

        self.accepted += 1
        return {"decision": "accept", "reasons": [], "size_scale": size_scale,
                "exploration": False, "bucket": bucket}

    def report(self, *, evidence: Optional[PreTradeEvidence] = None) -> dict:
        out = {
            "enabled": self.enabled,
            "min_score": self.min_score,
            "exploration_rate": self.exploration_rate,
            "min_size_scale": self.min_size_scale,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "explored": self.explored,
            "reject_reasons": dict(self.reject_reasons),
            "note": ("Synthesizes all data before trade; restrict-only; grades readiness buckets. "
                     "paper_only"),
        }
        if evidence is not None:
            rows = []
            for b in sorted(evidence.buckets):
                st = evidence.stat(b)
                if st:
                    rows.append({"bucket": b, **st, **self._assess_bucket(st)})
            out["bucket_evidence"] = rows
        return out

    def to_state(self) -> dict:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "explored": self.explored,
            "reject_reasons": dict(self.reject_reasons),
        }

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.accepted = int(data.get("accepted", 0) or 0)
        self.rejected = int(data.get("rejected", 0) or 0)
        self.explored = int(data.get("explored", 0) or 0)
        self.reject_reasons = {k: int(v or 0) for k, v in (data.get("reject_reasons") or {}).items()}
