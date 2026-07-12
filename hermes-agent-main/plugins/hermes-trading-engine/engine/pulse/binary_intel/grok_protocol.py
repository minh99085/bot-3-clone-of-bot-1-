"""Grok compute protocols — structured prompts that use binary_intel math + 5m TV.

Pre-trade: deep analysis of data at hand before fill (shadow-mode by default).
Post-trade: autopsy that advances lessons / loop memory.

These protocols do NOT call xAI themselves — they shape payloads for GrokDecider /
GrokSignalAnalyst so existing budget + shadow grading stay authoritative.
"""

from __future__ import annotations

from typing import Optional


PRE_TRADE_SYSTEM = (
    "You are a quantitative researcher for Polymarket BTC/ETH binary up/down markets. "
    "Settlement: Chainlink close >= open → Up. Use ONLY provided numerics. "
    "Apply binary digital option math (d₂, theta, entropy, Kelly with estimation error) "
    "and 5m RSI divergence as confirm/fade — never as primary trend (trend = price_action). "
    "Output JSON: {side: up|down|no_trade, conviction: 0-1, rsi_overlay: confirm|fade|noop, "
    "rationale: string<=280}."
)

POST_TRADE_SYSTEM = (
    "You are grading a settled Polymarket binary trade for loop learning. "
    "Compare pre-trade binary_intel scores and 5m RSI lean vs outcome. "
    "Extract one retractable lesson (avoid/exploit). "
    "Output JSON: {lesson_kind: avoid|exploit|risk, lesson_key: string, "
    "lesson_rule: string<=240, grade: correct|wrong|lucky}."
)


def build_pre_trade_grok_payload(
    *,
    binary_intel: dict,
    bundle_excerpt: Optional[dict] = None,
) -> dict:
    """Payload for Grok deep pre-trade compute (tier: deep when intel mid+TV conflict)."""
    brief = (binary_intel or {}).get("grok_brief") or {}
    tv = (binary_intel or {}).get("tv_universal") or {}
    math = (binary_intel or {}).get("math") or {}
    conflict = (tv.get("cross_asset") or {}).get("status") == "conflict"
    fade = (tv.get("decision") or {}).get("decision") == "fade"
    tier = "deep" if (conflict or fade or float(binary_intel.get("composite_score") or 0) < 0.45) else "full"
    return {
        "protocol": "binary_intel_pre_trade/v1",
        "compute_tier": tier,
        "system": PRE_TRADE_SYSTEM,
        "user": {
            "binary_intel_brief": brief,
            "math": {
                "z": math.get("displacement_z"),
                "d2": math.get("d2"),
                "theta": math.get("theta_per_sec"),
                "uncertainty": math.get("market_uncertainty"),
                "rsi_ig": math.get("rsi_information_gain"),
                "convergence": math.get("convergence"),
                "kelly": math.get("kelly"),
                "intelligence_score": math.get("intelligence_score"),
            },
            "tv_5m": {
                "lane": tv.get("lane"),
                "asset": tv.get("asset"),
                "btc_lean": ((tv.get("btc") or {}).get("lean")),
                "eth_lean": ((tv.get("eth") or {}).get("lean")),
                "cross_asset": tv.get("cross_asset"),
                "decision": tv.get("decision"),
            },
            "bundle_excerpt": bundle_excerpt,
        },
        "response_schema": {
            "side": "up|down|no_trade",
            "conviction": "float 0-1",
            "rsi_overlay": "confirm|fade|noop",
            "rationale": "string",
        },
    }


def build_post_trade_grok_payload(*, autopsy: dict) -> dict:
    return {
        "protocol": "binary_intel_post_trade/v1",
        "compute_tier": "full",
        "system": POST_TRADE_SYSTEM,
        "user": autopsy,
        "response_schema": {
            "lesson_kind": "avoid|exploit|risk",
            "lesson_key": "string",
            "lesson_rule": "string",
            "grade": "correct|wrong|lucky",
        },
    }


def should_request_pre_trade_grok(binary_intel: dict, *, enabled: bool = True) -> bool:
    if not enabled or not binary_intel:
        return False
    # Spend Grok compute when setup is non-trivial or TV conflicts
    score = float(binary_intel.get("composite_score") or 0.5)
    tv = binary_intel.get("tv_universal") or {}
    if (tv.get("cross_asset") or {}).get("status") == "conflict":
        return True
    if (tv.get("decision") or {}).get("decision") == "fade":
        return True
    if 0.35 <= score <= 0.72:
        return True  # ambiguous band — Grok adds most value
    return False
