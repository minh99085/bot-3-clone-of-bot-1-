"""Universal 5m RSI Divergence feed — ALL lanes + BTC/ETH symbols.

Bot 3 runs exactly two TV charts (INDEX:BTCUSD + INDEX:ETHUSD @ 5m).
This module makes those alerts available to every directional lane (5m/15m/1h)
and both assets, with cross-asset agreement scoring for Grok + sizing.
"""

from __future__ import annotations

from typing import Optional

from engine.pulse.tv_rsi_overlay import (
    latest_rsi_overlay,
    resolve_rsi_overlay_from_intake,
    rsi_overlay_decision,
    size_mult_for_rsi_overlay,
)


# Bot 3 chart symbols per lane: INDEX *USD (15m/Chainlink) + *USDT (1h/Binance).
# Both are candidates so the universal 5m feed resolves whichever lane fired.
BTC_SYMBOLS = ("BTCUSD", "INDEX:BTCUSD", "BTC", "BTCUSDT", "BINANCE:BTCUSDT")
ETH_SYMBOLS = ("ETHUSD", "INDEX:ETHUSD", "ETH", "ETHUSDT", "BINANCE:ETHUSDT")


def _asset_from_window(window) -> str:
    slug = str(getattr(window, "series_slug", "") or "").lower()
    label = str(getattr(window, "series_label", "") or "").lower()
    if "eth" in slug or "eth" in label or "ethereum" in slug:
        return "eth"
    return "btc"


def _lane_from_window(window) -> str:
    ws = int(getattr(window, "window_seconds", 900) or 900)
    if ws >= 3600:
        return "1h"
    if ws >= 600:
        return "15m"
    return "5m"


def _resolve_for_cands(intake, candidates: tuple, *, now: float,
                      max_age_s: float) -> Optional[dict]:
    if intake is None:
        return None
    for cand in candidates:
        try:
            rows = list(intake.rsi_div_history_for_symbol(cand) or [])
        except Exception:  # noqa: BLE001
            rows = []
        ov = latest_rsi_overlay(rows, now=float(now), max_age_s=float(max_age_s))
        if ov:
            return {**ov, "resolved_symbol": cand}
    # Fall back to path-aware resolver (handles BINANCE aliases if present)
    for cand in candidates[:1]:
        ov = resolve_rsi_overlay_from_intake(
            intake, cand, now=float(now), max_age_s=float(max_age_s))
        if ov:
            return ov
    return None


def cross_asset_agreement(btc: Optional[dict], eth: Optional[dict]) -> dict:
    """Score BTC vs ETH 5m RSI divergence agreement."""
    b_lean = str((btc or {}).get("lean") or "").lower() or None
    e_lean = str((eth or {}).get("lean") or "").lower() or None
    if not b_lean and not e_lean:
        return {"status": "silent", "lean": None, "agreement": None, "score": 0.5}
    if b_lean and e_lean:
        if b_lean == e_lean:
            return {"status": "agree", "lean": b_lean, "agreement": True, "score": 1.0}
        return {"status": "conflict", "lean": None, "agreement": False, "score": 0.2}
    only = b_lean or e_lean
    return {"status": "single_asset", "lean": only, "agreement": None, "score": 0.65}


def universal_tv_snapshot(
    intake,
    *,
    window=None,
    asset: Optional[str] = None,
    now: float,
    max_age_s: float = 2700.0,
    proposed_side: Optional[str] = None,
    aligned_mult: float = 1.15,
    opposed_mult: float = 0.45,
) -> dict:
    """5m RSI divergence for both symbols + lane-aware focus lean.

    Used by every lane: the same 5m INDEX alerts teach 15m and 1h entries.
    """
    asset_l = (asset or (_asset_from_window(window) if window is not None else "btc")).lower()
    lane = _lane_from_window(window) if window is not None else "15m"

    btc = _resolve_for_cands(intake, BTC_SYMBOLS, now=now, max_age_s=max_age_s)
    eth = _resolve_for_cands(intake, ETH_SYMBOLS, now=now, max_age_s=max_age_s)
    x = cross_asset_agreement(btc, eth)

    focus = btc if asset_l == "btc" else eth
    focus_lean = str((focus or {}).get("lean") or "").lower() or None
    # When focus silent, borrow cross-asset lean (same macro move often hits both)
    effective_lean = focus_lean or x.get("lean")
    decision = rsi_overlay_decision(
        side=proposed_side,
        overlay={"lean": effective_lean} if effective_lean else None,
    )
    size_mult = size_mult_for_rsi_overlay(
        side=proposed_side,
        overlay={"lean": effective_lean} if effective_lean else None,
        aligned_mult=aligned_mult,
        opposed_mult=opposed_mult,
    )
    # Cross-asset conflict → extra haircut; agreement → mild boost
    if x.get("status") == "conflict":
        size_mult *= 0.75
    elif x.get("status") == "agree" and decision.get("decision") == "confirm":
        size_mult *= 1.08

    return {
        "enabled": True,
        "source": "tv_5m_rsi_divergence_universal",
        "lane": lane,
        "asset": asset_l,
        "btc": btc,
        "eth": eth,
        "cross_asset": x,
        "focus": focus,
        "effective_lean": effective_lean,
        "decision": decision,
        "size_mult": round(float(size_mult), 4),
        "note": (
            "Same INDEX 5m RSI divergence alerts feed ALL lanes (5m/15m/1h) and both "
            "assets. Confirm/fade only — never overrides Chainlink price_action_trend."
        ),
    }
