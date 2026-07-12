"""RSI 30/70 band analysis — continuous overbought/oversold context for bot + Grok.

Separate FIFO from bar-close path and RSI divergence overlay. Bar-close heartbeats
from Hermes RSI indicator carry ``signal_kind=rsi_band`` with zone + cross events.
"""

from __future__ import annotations

from typing import Optional

from engine.pulse.tv_15m_price_path import path_symbol_candidates

DEFAULT_OS = 30.0
DEFAULT_OB = 70.0


def classify_rsi_zone(
    rsi: Optional[float],
    *,
    oversold: float = DEFAULT_OS,
    overbought: float = DEFAULT_OB,
) -> Optional[str]:
    """Return oversold | neutral | overbought, or None if rsi missing."""
    if rsi is None:
        return None
    try:
        v = float(rsi)
    except (TypeError, ValueError):
        return None
    if v <= float(oversold):
        return "oversold"
    if v >= float(overbought):
        return "overbought"
    return "neutral"


def filter_rsi_band(alerts: list) -> list:
    rows = [a for a in (alerts or []) if isinstance(a, dict)]
    return [a for a in rows if str(a.get("signal_kind") or "").strip().lower() == "rsi_band"]


def _age_s(row: dict, now: float) -> Optional[float]:
    for key in ("received_at", "bar_time"):
        try:
            t = float(row.get(key))
        except (TypeError, ValueError):
            continue
        if t > 1e12:
            t /= 1000.0
        if t > 0:
            return max(0.0, float(now) - t)
    return None


def _row_ts(row: dict) -> float:
    try:
        t = float(row.get("received_at") or row.get("bar_time") or 0)
    except (TypeError, ValueError):
        return 0.0
    if t > 1e12:
        t /= 1000.0
    return t


def lean_from_zone(zone: Optional[str]) -> Optional[str]:
    """Mean-reversion lean: oversold→up, overbought→down."""
    z = str(zone or "").strip().lower()
    if z == "oversold":
        return "up"
    if z == "overbought":
        return "down"
    return None


def latest_rsi_band_row(
    alerts: list,
    *,
    now: float,
    max_age_s: float = 900.0,
) -> Optional[dict]:
    rows = filter_rsi_band(alerts)
    if not rows:
        return None
    best = None
    best_t = -1.0
    for a in rows:
        age = _age_s(a, now)
        if age is None or age > float(max_age_s):
            continue
        t = _row_ts(a)
        if t >= best_t:
            best_t = t
            best = a
    return best


def summarize_band_history(rows: list, *, oversold: float = DEFAULT_OS,
                           overbought: float = DEFAULT_OB) -> dict:
    """Compact stats from oldest→newest rsi_band rows."""
    band_rows = filter_rsi_band(rows)
    if not band_rows:
        return {"n": 0}
    zones = []
    crosses = []
    rsi_vals = []
    for r in band_rows:
        z = str(r.get("rsi_zone") or classify_rsi_zone(r.get("rsi"),
                                                         oversold=oversold,
                                                         overbought=overbought) or "")
        if z:
            zones.append(z)
        ev = str(r.get("band_event") or "").strip().lower()
        if ev and ev != "none":
            crosses.append(ev)
        try:
            if r.get("rsi") is not None:
                rsi_vals.append(float(r["rsi"]))
        except (TypeError, ValueError):
            pass
    last = band_rows[-1]
    last_zone = str(last.get("rsi_zone") or classify_rsi_zone(
        last.get("rsi"), oversold=oversold, overbought=overbought) or "neutral")
    oversold_bars = sum(1 for z in zones if z == "oversold")
    overbought_bars = sum(1 for z in zones if z == "overbought")
    neutral_bars = sum(1 for z in zones if z == "neutral")
    return {
        "n": len(band_rows),
        "last_zone": last_zone,
        "last_rsi": last.get("rsi"),
        "last_band_event": last.get("band_event"),
        "oversold_bars": oversold_bars,
        "overbought_bars": overbought_bars,
        "neutral_bars": neutral_bars,
        "recent_crosses": crosses[-6:],
        "rsi_min": min(rsi_vals) if rsi_vals else None,
        "rsi_max": max(rsi_vals) if rsi_vals else None,
        "rsi_mean": (sum(rsi_vals) / len(rsi_vals)) if rsi_vals else None,
        "oversold_threshold": oversold,
        "overbought_threshold": overbought,
    }


def rsi_band_snapshot(
    rows: list,
    *,
    now: float,
    max_age_s: float = 900.0,
    oversold: float = DEFAULT_OS,
    overbought: float = DEFAULT_OB,
    history_n: int = 12,
) -> Optional[dict]:
    """Fresh RSI band context for Grok / MC / status."""
    latest = latest_rsi_band_row(rows, now=float(now), max_age_s=float(max_age_s))
    if latest is None:
        return None
    hist = filter_rsi_band(rows)[-max(1, int(history_n or 12)):]
    summary = summarize_band_history(hist, oversold=oversold, overbought=overbought)
    zone = str(latest.get("rsi_zone") or classify_rsi_zone(
        latest.get("rsi"), oversold=oversold, overbought=overbought) or "neutral")
    lean = lean_from_zone(zone)
    try:
        os_th = float(latest.get("rsi_os_threshold") or oversold)
        ob_th = float(latest.get("rsi_ob_threshold") or overbought)
    except (TypeError, ValueError):
        os_th, ob_th = oversold, overbought
    return {
        "rsi": latest.get("rsi"),
        "rsi_zone": zone,
        "band_event": latest.get("band_event"),
        "lean": lean,
        "direction": latest.get("direction"),
        "age_s": _age_s(latest, now),
        "oversold_threshold": os_th,
        "overbought_threshold": ob_th,
        "signal_level": latest.get("signal_level"),
        "symbol": latest.get("symbol"),
        "indicator_name": latest.get("indicator_name"),
        "history_summary": summary,
        "recent_bars": [
            {
                "rsi": r.get("rsi"),
                "rsi_zone": r.get("rsi_zone"),
                "band_event": r.get("band_event"),
                "bar_time": r.get("bar_time"),
            }
            for r in hist[-8:]
        ],
        "observe_only": True,
        "source": "rsi_band_5m",
        "note": (
            "RSI 30/70 band: oversold (<=%s) mean-revert UP lean; "
            "overbought (>=%s) mean-revert DOWN lean; neutral = no band lean. "
            "Separate from divergence overlay and price path."
            % (int(os_th), int(ob_th))
        ),
    }


def resolve_rsi_band_from_intake(
    intake,
    symbol: Optional[str],
    *,
    now: float,
    max_age_s: float = 900.0,
    history_n: int = 12,
) -> Optional[dict]:
    """RSI 30/70 band from the lane-routed symbol FIFO only."""
    if intake is None:
        return None
    for cand in path_symbol_candidates(symbol, strict_lane=True):
        try:
            rows = list(intake.rsi_band_history_for_symbol(cand) or [])
        except Exception:  # noqa: BLE001
            rows = []
        snap = rsi_band_snapshot(
            rows, now=float(now), max_age_s=float(max_age_s), history_n=history_n)
        if snap:
            return {**snap, "resolved_symbol": cand}
    return None
