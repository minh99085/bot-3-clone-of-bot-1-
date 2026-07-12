"""RSI Divergence overlay — confirm/fade only (never drives trend path).

Separate FIFO from bar-close. Regular bull/bear only; silent = no-op.
"""

from __future__ import annotations

from typing import Optional

from engine.pulse.tv_15m_price_path import path_symbol_candidates


def filter_rsi_divergence(alerts: list) -> list:
    rows = [a for a in (alerts or []) if isinstance(a, dict)]
    out = []
    for a in rows:
        kind = str(a.get("signal_kind") or "").strip().lower()
        level = str(a.get("signal_level") or "").strip().upper()
        div = str(a.get("divergence_kind") or "").strip().lower()
        if kind == "rsi_divergence":
            if div.startswith("hidden") or "HIDDEN" in level:
                continue
            out.append(a)
        elif level in ("REGULAR_BULL_DIV", "REGULAR_BEAR_DIV"):
            out.append(a)
    return out


def _age_s(row: dict, now: float) -> Optional[float]:
    for key in ("received_at", "bar_time"):
        try:
            t = float(row.get(key))
        except (TypeError, ValueError):
            continue
        if t > 1e12:  # ms
            t /= 1000.0
        if t > 0:
            return max(0.0, float(now) - t)
    return None


def latest_rsi_overlay(alerts: list, *, now: float, max_age_s: float = 2700.0) -> Optional[dict]:
    """Newest fresh regular RSI-div alert, or None."""
    rows = filter_rsi_divergence(alerts)
    if not rows:
        return None
    best = None
    best_t = -1.0
    for a in rows:
        age = _age_s(a, now)
        if age is None or age > float(max_age_s):
            continue
        try:
            t = float(a.get("received_at") or a.get("bar_time") or 0)
        except (TypeError, ValueError):
            t = 0.0
        if t > 1e12:
            t /= 1000.0
        if t >= best_t:
            best_t = t
            best = a
    if best is None:
        return None
    direction = str(best.get("direction") or "").upper()
    lean = "up" if direction == "UP" else ("down" if direction == "DOWN" else None)
    return {
        "lean": lean,
        "direction": direction,
        "signal_level": best.get("signal_level"),
        "divergence_kind": best.get("divergence_kind"),
        "strength": best.get("strength"),
        "rsi": best.get("rsi"),
        "age_s": _age_s(best, now),
        "price": best.get("price"),
        "n_history": len(rows),
        "observe_only": True,
        "source": "rsi_divergence_5m",
        "indicator_name": best.get("indicator_name"),
        "symbol": best.get("symbol"),
    }


def resolve_rsi_overlay_from_intake(intake, symbol: Optional[str], *, now: float,
                                   max_age_s: float = 2700.0) -> Optional[dict]:
    """RSI divergence overlay from the lane-routed symbol FIFO only."""
    if intake is None:
        return None
    for cand in path_symbol_candidates(symbol, strict_lane=True):
        try:
            rows = list(intake.rsi_div_history_for_symbol(cand) or [])
        except Exception:  # noqa: BLE001
            rows = []
        ov = latest_rsi_overlay(rows, now=float(now), max_age_s=float(max_age_s))
        if ov:
            return {**ov, "resolved_symbol": cand}
    return None


def size_mult_for_rsi_overlay(*, side: Optional[str], overlay: Optional[dict],
                              aligned_mult: float = 1.15,
                              opposed_mult: float = 0.45) -> float:
    if not side or not overlay:
        return 1.0
    lean = str(overlay.get("lean") or "").lower()
    if lean not in ("up", "down"):
        return 1.0
    side_l = str(side).lower()
    if lean == side_l:
        return float(aligned_mult)
    return float(opposed_mult)


def rsi_overlay_decision(*, side: Optional[str], overlay: Optional[dict]) -> dict:
    """Compact tag for research / soft gate."""
    if not overlay or not overlay.get("lean"):
        return {"decision": "noop", "reason": "no_fresh_rsi", "lean": None}
    lean = str(overlay.get("lean")).lower()
    side_l = str(side or "").lower()
    if side_l not in ("up", "down"):
        return {"decision": "noop", "reason": "no_side", "lean": lean}
    if lean == side_l:
        return {"decision": "confirm", "reason": "rsi_aligned", "lean": lean}
    return {"decision": "fade", "reason": "rsi_opposed", "lean": lean}
