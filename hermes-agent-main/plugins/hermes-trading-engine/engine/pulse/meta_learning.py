"""LLM (Grok/ChatGPT) BATCH meta-learning over the light report — DIAGNOSTICS ONLY.

Builds a compact JSON bundle (feature summaries, bucket PnL, execution stats, calibration,
missing-data reasons, learning questions) for offline/batch analysis. If a Grok integration
exists, it can return STRUCTURED diagnostics over that bundle; it NEVER makes live trade
decisions and never feeds the execution path. If no integration exists, the bundle artifact is
written and the report flags the integration as missing.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger("hte.pulse.meta_learning")

LEARNING_QUESTIONS = [
    "Which Hurst regime / Markov state shows the best clean win-rate and positive PnL?",
    "Which z-score, half-life, spread, depth, or time-to-resolution bucket is strongest?",
    "Which reject reason dominates, and is it destroying real edge or correctly blocking noise?",
    "Is the gap between EV-before-costs and EV-after-costs eroding edge?",
    "Are any buckets Tier A+ / A with sufficient clean sample, or is everything still observe-only?",
    "Where is missing data most limiting the analysis, and which feed should improve first?",
]


def build_bundle(light_report: dict) -> dict:
    """Compact, LLM-friendly summary of the latest light report (no raw rows)."""
    lr = light_report or {}
    pnl_by = {k: v for k, v in lr.items() if k.startswith("pnl_by_")}
    return {
        "schema": "btc_pulse_meta_bundle/1.0", "report_only": True,
        "no_live_trading_decisions": True,
        "candidate_lifecycle": lr.get("candidate_lifecycle"),
        "execution_stats": lr.get("execution_stats"),
        "reject_reasons": lr.get("reject_reasons"),
        "ev_before_after_costs": lr.get("ev_before_after_costs"),
        "calibration": lr.get("calibration"),
        "edge_model_calibration": lr.get("edge_model_calibration"),
        "sample_sizes": lr.get("sample_sizes"),
        "missing_data_reasons": lr.get("missing_data_reasons"),
        "tier_census": (lr.get("confidence_tier_table") or {}).get("tier_census"),
        "promotion_candidates": lr.get("promotion_candidates"),
        "demotion_candidates": lr.get("demotion_candidates"),
        "bucket_pnl": pnl_by,
        "learning_questions": list(LEARNING_QUESTIONS),
    }


_PROMPT = (
    "You are a quant research assistant analyzing a PAPER-trading BTC 5-minute Polymarket bot. "
    "Given this JSON report bundle, return STRICT JSON ONLY with keys: "
    '{"observations":[...],"hypotheses":[...],"suggested_experiments":[...],'
    '"data_gaps":[...]}. Do NOT make trade decisions, sizes, or buy/sell calls; diagnostics only.'
)


def grok_meta_diagnose(bundle: dict, *, assessor=None, integration_available: bool = False) -> dict:
    """Run a BATCH structured diagnosis over the bundle. ``assessor`` is a callable
    (prompt, bundle_json) -> dict|None (injectable for tests). Never returns trade decisions."""
    if not integration_available or assessor is None:
        return {"integration": "missing", "no_live_trading_decisions": True,
                "reason": "no_grok_integration_or_key", "diagnostic": None}
    try:
        raw = assessor(_PROMPT, json.dumps(bundle, default=str))
    except Exception:  # noqa: BLE001 — meta-learning never breaks the loop
        raw = None
    if not isinstance(raw, dict):
        return {"integration": "available", "no_live_trading_decisions": True,
                "reason": "no_structured_response", "diagnostic": None}
    # keep ONLY diagnostic keys; strip anything trade-like for safety
    safe = {k: raw.get(k) for k in ("observations", "hypotheses", "suggested_experiments",
                                    "data_gaps") if k in raw}
    return {"integration": "available", "no_live_trading_decisions": True,
            "reason": "ok", "diagnostic": safe}
