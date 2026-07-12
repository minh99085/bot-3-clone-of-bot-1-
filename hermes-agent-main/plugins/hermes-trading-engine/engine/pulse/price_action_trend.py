"""Spot price-action trend for BTC/ETH — rising/falling/flat from oracle prices.

Uses Chainlink RTDS (or proxy) spot vs window-open snapshot. Does NOT use TradingView
UP/DOWN alert labels. Contract sides (up/down) are aligned to trend only at triage time.
"""

from __future__ import annotations

import os
from typing import Any, Optional


TREND_RISING = "rising"
TREND_FALLING = "falling"
TREND_FLAT = "flat"
TREND_VALUES = (TREND_RISING, TREND_FALLING, TREND_FLAT)


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def trend_source_from_env() -> str:
    """``price`` (default) or ``tv`` for legacy TradingView alert direction."""
    return (os.getenv("PULSE_TRIAGE_TREND_SOURCE", "price") or "price").strip().lower()


def min_move_bps_from_env() -> float:
    return _envf("PULSE_PRICE_TREND_MIN_MOVE_BPS", 2.0)


def trend_aligns_side(trend: Optional[str], side: str) -> bool:
    """Map spot trend to Polymarket contract side (up/down tokens only)."""
    t = str(trend or "").strip().lower()
    if t == TREND_RISING:
        return side == "up"
    if t == TREND_FALLING:
        return side == "down"
    return False


def compute_trend_from_prices(
    *,
    asset: str,
    spot_now: Optional[float],
    open_price: Optional[float],
    price_age_s: Optional[float] = None,
    max_price_age_s: float = 30.0,
    min_move_bps: Optional[float] = None,
) -> Optional[dict]:
    """Derive rising/falling/flat from spot vs window open. Returns None when data is stale."""
    if min_move_bps is None:
        min_move_bps = min_move_bps_from_env()
    try:
        now_px = float(spot_now)
        open_px = float(open_price)
    except (TypeError, ValueError):
        return None
    if now_px <= 0 or open_px <= 0:
        return None
    if price_age_s is not None and float(price_age_s) > float(max_price_age_s):
        return None

    move_bps = 10000.0 * (now_px - open_px) / open_px
    thr = float(min_move_bps)
    if move_bps > thr:
        trend = TREND_RISING
    elif move_bps < -thr:
        trend = TREND_FALLING
    else:
        trend = TREND_FLAT

    strength = min(1.0, abs(move_bps) / max(thr * 4.0, 1.0))
    return {
        "source": "price_action",
        "asset": str(asset or "btc").lower(),
        "trend": trend,
        "strength": round(strength, 4),
        "timeframe": "spot",
        "spot_now": round(now_px, 4),
        "open_price": round(open_px, 4),
        "move_from_open_bps": round(move_bps, 3),
        "age_s": (round(float(price_age_s), 2) if price_age_s is not None else None),
    }


def to_triage_feature(trend: dict) -> dict:
    """Shape expected by AssetTriageSkill when trend_source=price."""
    return {
        "source": "price_action",
        "trend": trend.get("trend"),
        "strength": trend.get("strength", 0.0),
        "timeframe": "spot",
        "age_s": trend.get("age_s"),
        "asset": trend.get("asset"),
        "move_from_open_bps": trend.get("move_from_open_bps"),
    }


def trend_for_window(
    *,
    window: Any,
    price_feed: Any,
    now: float,
    max_price_age_s: float,
    min_move_bps: Optional[float] = None,
) -> Optional[dict]:
    """Window-scoped trend using the asset-matched oracle feed."""
    if price_feed is None or window is None:
        return None
    event_id = getattr(window, "event_id", None)
    if not event_id:
        return None
    price_feed.snapshot_open(event_id, getattr(window, "open_ts", 0), now=now)
    spot = price_feed.current()
    snap = price_feed.open_snapshot(event_id)
    if spot is None or snap is None:
        return None
    age = price_feed.age_s(now) if hasattr(price_feed, "age_s") else None
    slug = str(getattr(window, "series_slug", "") or "").lower()
    asset = "eth" if slug.startswith("eth") else "btc"
    raw = compute_trend_from_prices(
        asset=asset,
        spot_now=spot,
        open_price=snap.price,
        price_age_s=age,
        max_price_age_s=max_price_age_s,
        min_move_bps=min_move_bps,
    )
    if raw is None:
        return None
    if not getattr(price_feed, "is_fresh", lambda _a: True)(max_price_age_s, now):
        return None
    return raw


def dual_asset_snapshot(
    *,
    btc_feed: Any,
    eth_feed: Any,
    btc_window: Any = None,
    eth_window: Any = None,
    now: float,
    max_price_age_s: float,
    min_move_bps: Optional[float] = None,
) -> dict:
    """BTC + ETH spot trends for Grok bundle (no TV UP/DOWN)."""
    out: dict = {"source": "price_action", "min_move_bps": min_move_bps or min_move_bps_from_env()}
    for asset, feed, win in (("btc", btc_feed, btc_window), ("eth", eth_feed, eth_window)):
        if feed is None:
            out[asset] = None
            continue
        if win is not None:
            t = trend_for_window(
                window=win, price_feed=feed, now=now,
                max_price_age_s=max_price_age_s, min_move_bps=min_move_bps)
        else:
            spot = feed.current()
            age = max(0.0, float(now) - float(getattr(feed, "_last_ts", 0) or 0))
            t = compute_trend_from_prices(
                asset=asset,
                spot_now=spot,
                open_price=spot,
                price_age_s=age,
                max_price_age_s=max_price_age_s,
                min_move_bps=min_move_bps,
            )
            if t is not None:
                t = {**t, "trend": TREND_FLAT, "move_from_open_bps": 0.0,
                     "note": "no_active_window_open_snapshot"}
        out[asset] = t
    return out
