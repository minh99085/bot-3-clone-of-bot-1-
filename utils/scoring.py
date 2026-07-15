"""Liquidity and time-decay helpers for conviction scoring."""

from __future__ import annotations

import math


def liquidity_score(liquidity_usd: float, volume_24h: float = 0.0) -> float:
    """Map depth/volume → [0.15, 1.0]. Thin books score low (rank down)."""
    depth = max(0.0, float(liquidity_usd)) + 0.25 * max(0.0, float(volume_24h))
    # log1p so $50k ≈ 0.9+, $1k ≈ 0.55, $100 ≈ 0.35
    raw = math.log1p(depth) / math.log1p(75_000.0)
    return float(min(1.0, max(0.15, raw)))


def time_decay_factor(
    seconds_to_resolution: float,
    *,
    horizon_seconds: float = 900.0,
) -> float:
    """Prefer nearer resolutions for crypto HF; still keep some weight far out.

    factor ∈ [0.25, 1.0]; peaks when resolution is imminent but not past.
    """
    t = max(0.0, float(seconds_to_resolution))
    if t <= 0:
        return 0.25  # already resolving / unknown
    # Smooth bump: near horizon → high; very far → lower
    ratio = min(1.0, horizon_seconds / max(t, 1.0))
    return float(min(1.0, max(0.25, 0.35 + 0.65 * ratio)))


def conviction_score(
    q: float,
    p: float,
    conviction: float,
    liquidity: float,
    time_decay: float,
) -> float:
    """Ranking score (exact product formulation).

    conviction_score = |q - p| * (conviction - 0.5) * liquidity_score * time_decay_factor
    """
    return float(
        abs(q - p) * (conviction - 0.5) * float(liquidity) * float(time_decay)
    )
