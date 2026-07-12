"""Conservative capped/delayed Kelly sizing DIAGNOSTICS for the BTC 5-min pulse (paper-only).

Computes a *suggested* size from calibrated win prob, payout ratio, EV-after-execution, with a
half-Kelly fraction, a hard per-trade cap, a daily-loss cap, and a degradation penalty. It does
NOT change the actual paper size unless ``sizing_enabled`` is explicitly true (config default
false), preserving backward compatibility. Strictly NO martingale / NO averaging down: the
suggestion never increases after losses (the degradation penalty only ever reduces size).
"""

from __future__ import annotations

from typing import Optional


def kelly_fraction(p_win: Optional[float], price: Optional[float]) -> Optional[float]:
    """Full-Kelly fraction for a binary bought at ``price`` (pays $1 if it wins). >=0, clamped."""
    if p_win is None or price is None or price <= 0 or price >= 1:
        return None
    b = (1.0 - price) / price                 # payout odds (net win per unit staked)
    if b <= 0:
        return None
    f = p_win - (1.0 - p_win) / b
    return max(0.0, f)


def degradation_penalty(daily_loss_so_far: float, daily_loss_cap_usd: float) -> float:
    """1.0 normally, shrinking toward 0 as the day's realized loss approaches the cap. Only ever
    REDUCES size (never a martingale)."""
    if daily_loss_cap_usd <= 0:
        return 1.0
    used = max(0.0, daily_loss_so_far) / daily_loss_cap_usd
    return max(0.0, min(1.0, 1.0 - used))


def sizing_diagnostics(*, p_win: Optional[float], price: Optional[float],
                       ev_after_costs: Optional[float], bankroll_usd: float,
                       hard_cap_usd: float, daily_loss_cap_usd: float,
                       daily_loss_so_far: float, base_size_usd: float,
                       sizing_enabled: bool = False) -> dict:
    f = kelly_fraction(p_win, price)
    half = (0.5 * f) if f is not None else None
    raw = (half * bankroll_usd) if half is not None else None
    capped = (min(raw, hard_cap_usd) if raw is not None else None)
    pen = degradation_penalty(daily_loss_so_far, daily_loss_cap_usd)
    penalized = (capped * pen) if capped is not None else None
    daily_cap_hit = daily_loss_so_far >= daily_loss_cap_usd > 0
    # EV must be positive for any suggestion; daily cap forces 0
    suggested = 0.0
    if penalized is not None and (ev_after_costs is None or ev_after_costs > 0) and not daily_cap_hit:
        suggested = max(0.0, penalized)
    # ACTUAL size only changes when explicitly enabled; default off -> base size unchanged
    actual = round(suggested, 2) if (sizing_enabled and not daily_cap_hit) else base_size_usd
    b = ((1.0 - price) / price) if (price and 0 < price < 1) else None
    return {"observe_only": not sizing_enabled, "sizing_enabled": bool(sizing_enabled),
            "promotion_gated": False,
            "calibrated_p_win": p_win,
            "payout_ratio": (round(b, 4) if b is not None else None),
            "ev_after_costs": ev_after_costs,
            "kelly_fraction": (round(f, 4) if f is not None else None),
            "half_kelly": (round(half, 4) if half is not None else None),
            "hard_cap_usd": hard_cap_usd, "daily_loss_cap_usd": daily_loss_cap_usd,
            "daily_loss_so_far": round(daily_loss_so_far, 4), "daily_cap_hit": daily_cap_hit,
            "degradation_penalty": round(pen, 4),
            "suggested_size_usd": round(suggested, 2), "actual_size_usd": actual,
            "no_martingale": True, "no_averaging_down": True}


def bucket_promoted(sel_tags: dict, *, is_promoted) -> bool:
    """True if ANY candidate bucket cleared the Wilson promotion scorecard."""
    for dim, val in (sel_tags or {}).items():
        if dim == "direction" or val is None:
            continue
        if is_promoted(str(dim), str(val)):
            return True
    return is_promoted("direction", str((sel_tags or {}).get("direction") or ""))


def sizing_diagnostics_promoted(
    *,
    sel_tags: dict,
    is_promoted,
    p_win: Optional[float],
    price: Optional[float],
    ev_after_costs: Optional[float],
    bankroll_usd: float,
    hard_cap_usd: float,
    daily_loss_cap_usd: float,
    daily_loss_so_far: float,
    base_size_usd: float,
    global_sizing_enabled: bool = False,
) -> dict:
    """Half-Kelly only when a bucket is promoted; otherwise flat base size (WS3)."""
    promoted = bucket_promoted(sel_tags, is_promoted=is_promoted)
    enabled = bool(global_sizing_enabled and promoted)
    out = sizing_diagnostics(
        p_win=p_win, price=price, ev_after_costs=ev_after_costs,
        bankroll_usd=bankroll_usd, hard_cap_usd=hard_cap_usd,
        daily_loss_cap_usd=daily_loss_cap_usd, daily_loss_so_far=daily_loss_so_far,
        base_size_usd=base_size_usd, sizing_enabled=enabled)
    out["promotion_gated"] = True
    out["bucket_promoted"] = promoted
    out["note"] = ("Kelly sizing only when bucket_promoted; flat size otherwise "
                   "(no martingale).")
    return out


def decide_trade_size(
    *,
    p_win: Optional[float],
    price: Optional[float],
    ev_after_costs: Optional[float],
    bankroll_usd: float,
    hard_cap_usd: float,
    daily_loss_cap_usd: float,
    daily_loss_so_far: float,
    base_size_usd: float,
    min_size_usd: float = 1.0,
    readiness_scale: float = 1.0,
    sizing_enabled: bool = True,
) -> dict:
    """Autonomous bet-size decision for Osmani (and similar) paper fills.

    Combines half-Kelly (edge × bankroll) with pre-trade readiness scale.
    Rules (PAPER ONLY, no martingale):
      * sizing OFF → flat base_size
      * daily loss cap hit → size 0 (skip)
      * Kelly > 0 → use Kelly, then × readiness_scale
      * Kelly == 0 but EV > 0 → fall back to base × readiness (still trade unit)
      * clamp to [min_size_usd, hard_cap_usd] when size > 0
      * readiness_scale never upsizes above 1.0 (restrict-only)
    """
    base = max(0.0, float(base_size_usd))
    hard = max(0.0, float(hard_cap_usd))
    floor = max(0.0, float(min_size_usd))
    scale = max(0.0, min(1.0, float(readiness_scale)))
    diag = sizing_diagnostics(
        p_win=p_win, price=price, ev_after_costs=ev_after_costs,
        bankroll_usd=bankroll_usd, hard_cap_usd=hard_cap_usd,
        daily_loss_cap_usd=daily_loss_cap_usd, daily_loss_so_far=daily_loss_so_far,
        base_size_usd=base, sizing_enabled=bool(sizing_enabled))
    if not sizing_enabled:
        return {
            **diag,
            "decision": "flat_base",
            "readiness_scale": scale,
            "min_size_usd": floor,
            "size_usd": round(base, 2),
            "autonomous": False,
        }
    if diag.get("daily_cap_hit"):
        return {
            **diag,
            "decision": "daily_loss_cap",
            "readiness_scale": scale,
            "min_size_usd": floor,
            "size_usd": 0.0,
            "autonomous": True,
        }
    suggested = float(diag.get("suggested_size_usd") or 0.0)
    ev_ok = ev_after_costs is None or float(ev_after_costs) > 0
    if suggested > 0:
        raw = suggested * scale
        decision = "kelly_x_readiness"
    elif ev_ok:
        raw = base * scale
        decision = "base_x_readiness"
    else:
        return {
            **diag,
            "decision": "no_edge",
            "readiness_scale": scale,
            "min_size_usd": floor,
            "size_usd": 0.0,
            "autonomous": True,
        }
    if hard > 0:
        raw = min(raw, hard)
    if raw > 0 and floor > 0:
        raw = max(raw, min(floor, hard if hard > 0 else floor))
    size = round(max(0.0, raw), 2)
    return {
        **diag,
        "decision": decision,
        "readiness_scale": scale,
        "min_size_usd": floor,
        "size_usd": size,
        "autonomous": True,
        "actual_size_usd": size,
    }
