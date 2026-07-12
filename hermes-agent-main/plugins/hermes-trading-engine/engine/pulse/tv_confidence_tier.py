"""TradingView confidence tier — observe-only param modulation (not a trade gate).

TV alerts update regime state; at 15m sweet-spot evaluation this module adjusts
``min_edge`` / ``max_price`` within safe bounds. It can only tune aggressiveness;
it never forces or blocks a trade outright (restrict-only deltas).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _strength(tv: dict) -> Optional[float]:
    try:
        return float(tv.get("strength"))
    except (TypeError, ValueError):
        return None


def cohort_ttc_band(
    *,
    ttc_s: Optional[float],
    window_seconds: int,
    ttc_min_base: float,
    ttc_max_base: float,
    fast_lane_15m: bool,
    ttc_min_15m: float,
    ttc_max_15m: float,
) -> tuple[Optional[float], Optional[float], bool]:
    """Return (ttc_min, ttc_max, fast_lane_active) for the window."""
    ws = int(window_seconds or 300)
    fast = bool(fast_lane_15m and ws >= 900)
    scale = float(ws) / 300.0
    if fast:
        return ttc_min_15m * scale, ttc_max_15m * scale, True
    return ttc_min_base * scale, ttc_max_base * scale, False


def in_sweet_spot(
    ttc_s: Optional[float],
    *,
    window_seconds: int,
    ttc_min_base: float,
    ttc_max_base: float,
    fast_lane_15m: bool,
    ttc_min_15m: float,
    ttc_max_15m: float,
) -> bool:
    if ttc_s is None:
        return False
    tmin, tmax, _ = cohort_ttc_band(
        ttc_s=ttc_s,
        window_seconds=window_seconds,
        ttc_min_base=ttc_min_base,
        ttc_max_base=ttc_max_base,
        fast_lane_15m=fast_lane_15m,
        ttc_min_15m=ttc_min_15m,
        ttc_max_15m=ttc_max_15m,
    )
    if tmin is None or tmax is None:
        return False
    t = float(ttc_s)
    return tmin <= t <= tmax


@dataclass(frozen=True)
class TvTierParams:
    enabled: bool = True
    require_sweet_spot: bool = True
    only_15m: bool = True
    aligned_strength_min: float = 0.72
    tier_a_min_edge_delta: float = -0.005
    tier_a_max_price_delta: float = 0.02
    tier_c_min_edge_delta: float = 0.005
    tier_c_max_price_delta: float = -0.03
    min_edge_floor: float = 0.01
    max_price_floor: float = 0.50
    max_price_ceiling: float = 0.85
    ttc_min_base: float = 180.0
    ttc_max_base: float = 240.0
    fast_lane_15m: bool = True
    ttc_min_15m: float = 160.0
    ttc_max_15m: float = 220.0


def classify_tv_tier(*, side: str, tv_feature: Optional[dict], params: TvTierParams) -> str:
    """Classify TV regime for proposed side: A (aligned), B (neutral), C (opposed)."""
    side = str(side or "").lower()
    tv = tv_feature or {}
    if side not in ("up", "down"):
        return "B"

    confirm_mtf = str(tv.get("tf_confirm_mtf") or "").lower()
    confirm_fast = str(tv.get("tf_confirm") or "").lower()
    mtf_align = str(tv.get("mtf_alignment") or "").lower()
    direction = str(tv.get("direction") or "").upper()
    signal_level = str(tv.get("signal_level") or "").upper()
    strength = _strength(tv) or 0.0
    fresh = int(tv.get("trend_fresh_count") or 0)

    want = "DOWN" if side == "down" else "UP"
    oppose = "UP" if side == "down" else "DOWN"

    aligned = False
    if want == "DOWN":
        if confirm_mtf.startswith("confirmed_down"):
            aligned = True
        elif confirm_fast == "confirmed_down":
            aligned = True
        elif mtf_align == "bearish_aligned":
            aligned = True
        elif direction == "DOWN" and strength >= params.aligned_strength_min:
            aligned = True
        elif confirm_mtf.startswith("partial_down") and fresh >= 2:
            aligned = True
    else:
        if confirm_mtf.startswith("confirmed_up"):
            aligned = True
        elif confirm_fast == "confirmed_up":
            aligned = True
        elif mtf_align == "bullish_aligned":
            aligned = True
        elif direction == "UP" and strength >= params.aligned_strength_min:
            aligned = True

    opposed = False
    if oppose == "UP":
        if signal_level == "UP_STRONG":
            opposed = True
        elif direction == "UP" and strength >= 0.75:
            opposed = True
        elif confirm_mtf.startswith("confirmed_up"):
            opposed = True
        elif mtf_align == "bullish_aligned" and direction == "UP":
            opposed = True
    else:
        if signal_level == "DOWN_STRONG":
            opposed = True
        elif direction == "DOWN" and strength >= 0.75:
            opposed = True
        elif confirm_mtf.startswith("confirmed_down"):
            opposed = True
        elif mtf_align == "bearish_aligned" and direction == "DOWN":
            opposed = True

    if aligned and not opposed:
        return "A"
    if opposed and not aligned:
        return "C"
    if aligned and opposed:
        return "B"
    return "B"


def resolve_tv_entry_params(
    *,
    side: str,
    tv_feature: Optional[dict],
    ttc_s: Optional[float],
    window_seconds: int,
    base_min_edge: float,
    base_max_price: float,
    params: TvTierParams,
) -> dict:
    """Compute effective entry params and metadata for logging."""
    ws = int(window_seconds or 300)
    base = {
        "enabled": bool(params.enabled),
        "observe_only": True,
        "tier": "base",
        "applied": False,
        "side": str(side or "").lower(),
        "min_edge": round(float(base_min_edge), 4),
        "max_price": round(float(base_max_price), 4),
        "base_min_edge": round(float(base_min_edge), 4),
        "base_max_price": round(float(base_max_price), 4),
        "min_edge_delta": 0.0,
        "max_price_delta": 0.0,
        "ttc_s": round(float(ttc_s), 1) if ttc_s is not None else None,
        "window_seconds": ws,
        "in_sweet_spot": False,
        "tv_confirm_mtf": (tv_feature or {}).get("tf_confirm_mtf"),
        "tv_direction": (tv_feature or {}).get("direction"),
        "tv_strength": _strength(tv_feature or {}),
        "note": "TV confidence tier modulates min_edge/max_price only; never forces or blocks.",
    }

    if not params.enabled:
        base["note"] = "disabled"
        return base

    if params.only_15m and ws < 900:
        base["note"] = "skipped_non_15m"
        return base

    sweet = in_sweet_spot(
        ttc_s,
        window_seconds=ws,
        ttc_min_base=params.ttc_min_base,
        ttc_max_base=params.ttc_max_base,
        fast_lane_15m=params.fast_lane_15m,
        ttc_min_15m=params.ttc_min_15m,
        ttc_max_15m=params.ttc_max_15m,
    )
    base["in_sweet_spot"] = sweet
    if params.require_sweet_spot and not sweet:
        base["note"] = "outside_sweet_spot"
        return base

    tier = classify_tv_tier(side=side, tv_feature=tv_feature, params=params)
    base["tier"] = tier

    edge_delta = 0.0
    price_delta = 0.0
    if tier == "A":
        edge_delta = float(params.tier_a_min_edge_delta)
        price_delta = float(params.tier_a_max_price_delta)
        base["reason"] = "tv_aligned_regime"
    elif tier == "C":
        edge_delta = float(params.tier_c_min_edge_delta)
        price_delta = float(params.tier_c_max_price_delta)
        base["reason"] = "tv_opposed_regime"
    else:
        base["reason"] = "tv_neutral_regime"
        return base

    eff_edge = _clamp(
        float(base_min_edge) + edge_delta,
        params.min_edge_floor,
        0.25,
    )
    eff_price = _clamp(
        float(base_max_price) + price_delta,
        params.max_price_floor,
        params.max_price_ceiling,
    )
    base.update({
        "applied": True,
        "min_edge_delta": round(edge_delta, 4),
        "max_price_delta": round(price_delta, 4),
        "min_edge": round(eff_edge, 4),
        "max_price": round(eff_price, 4),
    })
    return base


def params_from_engine_cfg(cfg) -> TvTierParams:
    """Build :class:`TvTierParams` from ``PulseConfig``-like object."""
    return TvTierParams(
        enabled=bool(getattr(cfg, "tv_confidence_tier_enabled", False)),
        require_sweet_spot=bool(getattr(cfg, "tv_tier_require_sweet_spot", True)),
        only_15m=bool(getattr(cfg, "tv_tier_15m_only", True)),
        aligned_strength_min=float(getattr(cfg, "tv_tier_aligned_strength_min", 0.72)),
        tier_a_min_edge_delta=float(getattr(cfg, "tv_tier_a_min_edge_delta", -0.005)),
        tier_a_max_price_delta=float(getattr(cfg, "tv_tier_a_max_price_delta", 0.02)),
        tier_c_min_edge_delta=float(getattr(cfg, "tv_tier_c_min_edge_delta", 0.005)),
        tier_c_max_price_delta=float(getattr(cfg, "tv_tier_c_max_price_delta", -0.03)),
        ttc_min_base=float(getattr(cfg, "baseline_cohort_ttc_min_s", 180.0)),
        ttc_max_base=float(getattr(cfg, "baseline_cohort_ttc_max_s", 240.0)),
        fast_lane_15m=bool(getattr(cfg, "baseline_cohort_15m_fast_lane", True)),
        ttc_min_15m=float(getattr(cfg, "baseline_cohort_15m_ttc_min_s", 160.0)),
        ttc_max_15m=float(getattr(cfg, "baseline_cohort_15m_ttc_max_s", 220.0)),
    )