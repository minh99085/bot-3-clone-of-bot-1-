"""Favorites + offline cell policy for Osmani path (PAPER ONLY).

Profile B from 30d walk-forward: prefer ask >= min_entry, cell Phase-2
FOLLOW boosts size, FADE blocks. Tags fills with ab_profile for A/B metrics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class FavoritesGateResult:
    allow: bool
    reason: str
    size_mult: float = 1.0
    cell_verdict: Optional[str] = None
    cell_key: Optional[str] = None
    ab_profile: str = "throughput"


def ab_profile_from_env() -> str:
    return (os.getenv("PULSE_AB_PROFILE", "throughput") or "throughput").strip().lower()


def favorites_policy_active() -> bool:
    prof = ab_profile_from_env()
    if prof in ("favorites", "favorites_wr", "profile_b", "b"):
        return True
    return (os.getenv("PULSE_FAVORITES_POLICY", "0") or "0").strip().lower() in (
        "1", "true", "yes", "on")


def min_entry_price_from_env() -> float:
    try:
        return float(os.getenv("PULSE_MIN_ENTRY_PRICE", "0.58") or 0.58)
    except (TypeError, ValueError):
        return 0.58


def cell_phase2_block_fade() -> bool:
    return (os.getenv("PULSE_CELL_PHASE2_BLOCK_FADE", "1") or "1").strip().lower() in (
        "1", "true", "yes", "on")


def evaluate_osmani_fill(
    *,
    side: str,
    ask: float,
    window: Any,
    now: float,
    cell_learning: Any = None,
    cell_phase2_enabled: bool = False,
) -> FavoritesGateResult:
    """Gate/size adjust an Osmani verified fill under favorites profile."""
    prof = ab_profile_from_env()
    if not favorites_policy_active():
        return FavoritesGateResult(allow=True, reason="policy_off", ab_profile=prof)

    try:
        ask_f = float(ask)
    except (TypeError, ValueError):
        return FavoritesGateResult(allow=False, reason="bad_ask", ab_profile=prof)

    floor = min_entry_price_from_env()
    if floor > 0 and ask_f < floor:
        return FavoritesGateResult(
            allow=False,
            reason="favorites_min_entry",
            ab_profile=prof,
        )

    size_mult = 1.0
    cell_verdict = None
    cell_key = None

    if cell_phase2_enabled and cell_learning is not None:
        try:
            from engine.pulse.directional_cell_learning import CellKey
            from engine.pulse.signal_edge import FADE, FOLLOW

            sso = float(getattr(window, "seconds_since_open", lambda _n: 0)(now))
            slug = str(getattr(window, "series_slug", "") or "").lower()
            lane = "1h" if "hourly" in slug or int(getattr(window, "window_seconds", 900) or 900) >= 3600 else "15m"
            asset = "eth" if slug.startswith("eth") else "btc"
            from engine.pulse.directional_cell_learning import (
                ask_band_from_price,
                minute_band_from_seconds,
            )
            ck = CellKey(
                asset=asset,
                horizon=lane,
                side=str(side).lower(),
                minute_band=minute_band_from_seconds(sso),
                regime="unknown",
                tv_pattern="∅",
                ask_band=ask_band_from_price(ask_f),
            )
            adj = cell_learning.phase2_adjustment(ck)
            cell_verdict = str(adj.get("verdict") or "")
            cell_key = ck.as_str()
            size_mult = float(adj.get("size_mult") or 1.0)
            n_trades = int(adj.get("trades") or 0)

            if cell_verdict == FADE and cell_phase2_block_fade() and n_trades >= int(
                    getattr(cell_learning, "min_samples", 8) or 8):
                return FavoritesGateResult(
                    allow=False,
                    reason="cell_phase2_fade",
                    size_mult=size_mult,
                    cell_verdict=cell_verdict,
                    cell_key=cell_key,
                    ab_profile=prof,
                )
            if cell_verdict == FOLLOW:
                size_mult = max(size_mult, 1.0)
        except Exception:  # noqa: BLE001
            pass

    return FavoritesGateResult(
        allow=True,
        reason="favorites_ok",
        size_mult=size_mult,
        cell_verdict=cell_verdict,
        cell_key=cell_key,
        ab_profile=prof,
    )


def ledger_ab_stats(positions: dict) -> dict:
    """WR/PnL by research.ab_profile for A/B comparison."""
    buckets: dict[str, dict] = {}

    def _b(name: str) -> dict:
        if name not in buckets:
            buckets[name] = {"n": 0, "wins": 0, "pnl": 0.0, "open": 0}
        return buckets[name]

    for pos in (positions or {}).values():
        if hasattr(pos, "status"):
            st = pos.status
            won = pos.won
            pnl = float(pos.pnl_usd or 0)
            rt = pos.research or {}
        else:
            st = pos.get("status")
            won = pos.get("won")
            pnl = float(pos.get("pnl_usd") or 0)
            rt = pos.get("research") or {}
        prof = str(rt.get("ab_profile") or "legacy")
        b = _b(prof)
        if st == "open":
            b["open"] += 1
            continue
        if st != "settled":
            continue
        b["n"] += 1
        if won:
            b["wins"] += 1
        b["pnl"] = round(float(b["pnl"]) + pnl, 2)

    out = {}
    for prof, b in sorted(buckets.items()):
        n = b["n"]
        out[prof] = {
            **b,
            "wr": round(b["wins"] / n, 4) if n else None,
            "pnl": b["pnl"],
        }
    return out
