"""Pre-trade binary intelligence script — synthesize math + TV + readiness for Grok.

Runs BEFORE every directional fill (legacy + Osmani). Restrict-only:
  * size_mult ∈ [min_scale, 1.25] (can mildly boost confirmed setups)
  * hard_block only when intelligence is critically weak (exploration carve-out)
  * emits Grok compute brief for deep analysis

Never bypasses evaluate_execution().
"""

from __future__ import annotations

import random
from typing import Optional

from engine.pulse.binary_intel.math_core import compute_binary_snapshot
from engine.pulse.binary_intel.tv_universal import universal_tv_snapshot


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def run_pre_trade_intel(
    *,
    intake=None,
    window=None,
    s_now: Optional[float] = None,
    s_open: Optional[float] = None,
    sigma_per_sec: Optional[float] = None,
    ttc_s: float = 0.0,
    window_seconds: float = 900.0,
    poly_mid: Optional[float] = None,
    model_p_up: Optional[float] = None,
    proposed_side: Optional[str] = None,
    ask: Optional[float] = None,
    now: float,
    readiness_score: Optional[float] = None,
    p_uncertainty: float = 0.0,
    max_age_s: float = 2700.0,
    kelly_fraction: float = 0.25,
    aligned_mult: float = 1.15,
    opposed_mult: float = 0.45,
    min_intel_score: float = 0.28,
    exploration_rate: float = 0.05,
    min_size_scale: float = 0.40,
) -> dict:
    """Invented pre-trade script: binary math × universal 5m TV × readiness."""
    tv = universal_tv_snapshot(
        intake,
        window=window,
        now=float(now),
        max_age_s=float(max_age_s),
        proposed_side=proposed_side,
        aligned_mult=aligned_mult,
        opposed_mult=opposed_mult,
    )
    rsi_lean = tv.get("effective_lean")
    rsi_strength = float(((tv.get("focus") or {}).get("strength") or 0.75))

    math_snap = compute_binary_snapshot(
        s_now=s_now,
        s_open=s_open,
        sigma_per_sec=sigma_per_sec,
        ttc_s=float(ttc_s),
        window_seconds=float(window_seconds),
        poly_mid=poly_mid,
        model_p_up=model_p_up,
        proposed_side=proposed_side,
        ask=ask,
        rsi_lean=rsi_lean,
        rsi_strength=rsi_strength,
        p_uncertainty=float(p_uncertainty),
        kelly_fraction=float(kelly_fraction),
    )

    intel = float(math_snap.get("intelligence_score") or 0.5)
    ready = float(readiness_score) if readiness_score is not None else 0.55
    tv_mult = float(tv.get("size_mult") or 1.0)

    # Blend: 55% binary intel, 30% readiness, 15% TV confirm strength
    tv_confirm = 1.0 if (tv.get("decision") or {}).get("decision") == "confirm" else (
        0.35 if (tv.get("decision") or {}).get("decision") == "fade" else 0.55)
    composite = 0.55 * intel + 0.30 * ready + 0.15 * tv_confirm

    # Size scale: base from composite, then × TV mult (clamped)
    base_scale = min_size_scale + (1.0 - min_size_scale) * composite
    size_mult = _clamp(base_scale * tv_mult, min_size_scale, 1.25)

    # Hard block only critical weakness (unless exploring)
    explore = random.random() < float(exploration_rate)
    hard_block = bool(composite < float(min_intel_score) and not explore)

    recommendation = (
        "trade" if composite >= 0.62 and not hard_block else
        ("cautious" if composite >= 0.45 and not hard_block else
         ("explore" if explore else "wait"))
    )

    grok_brief = {
        "role": "pre_trade_binary_intel",
        "task": (
            "Analyze this Polymarket binary setup BEFORE entry. Use displacement_z, "
            "theta, market entropy, RSI information gain, and convergence edge. "
            "Decide if side is supported; never invent prices. Output conviction 0–1 "
            "and confirm/fade vs 5m RSI lean."
        ),
        "lane": tv.get("lane"),
        "asset": tv.get("asset"),
        "proposed_side": proposed_side,
        "intelligence_score": round(intel, 4),
        "composite_score": round(composite, 4),
        "recommendation": recommendation,
        "key_signals": {
            "z": math_snap.get("displacement_z"),
            "theta": math_snap.get("theta_per_sec"),
            "entropy": (math_snap.get("market_uncertainty") or {}).get("entropy_bits"),
            "rsi_ig_bits": (math_snap.get("rsi_information_gain") or {}).get("info_gain_bits"),
            "rsi_lean": rsi_lean,
            "rsi_decision": (tv.get("decision") or {}).get("decision"),
            "cross_asset": (tv.get("cross_asset") or {}).get("status"),
            "convergence_edge": (math_snap.get("convergence") or {}).get("weighted_edge"),
            "kelly_f_adj": (math_snap.get("kelly") or {}).get("f_adj"),
        },
        "formulas_ref": math_snap.get("formulas"),
    }

    return {
        "enabled": True,
        "observe_only": True,
        "script": "binary_intel.pre_trade/v1",
        "math": math_snap,
        "tv_universal": tv,
        "intelligence_score": round(intel, 4),
        "composite_score": round(composite, 4),
        "readiness_score": (round(ready, 4) if readiness_score is not None else None),
        "size_mult": round(size_mult, 4),
        "hard_block": hard_block,
        "exploration": explore and hard_block is False and recommendation == "explore",
        "recommendation": recommendation,
        "grok_brief": grok_brief,
        "research_tags": {
            "binary_intel_score": round(composite, 4),
            "binary_intel_z": math_snap.get("displacement_z"),
            "binary_intel_rsi_lean": rsi_lean,
            "binary_intel_rsi_decision": (tv.get("decision") or {}).get("decision"),
            "tv_rsi_overlay_aligned": (
                True if (tv.get("decision") or {}).get("decision") == "confirm" else
                (False if (tv.get("decision") or {}).get("decision") == "fade" else None)
            ),
            "tv_cross_asset_rsi": (tv.get("cross_asset") or {}).get("status"),
        },
    }
