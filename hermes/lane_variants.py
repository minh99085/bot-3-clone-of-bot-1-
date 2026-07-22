"""Per-lane strategy variants — one hypothesis per lane, shared barrier core.

All 10 lanes trade the SAME market (btc-updown-15m), so every window is a
paired sample: market luck cancels out of lane-vs-lane comparisons and
differences are pure strategy signal. Controls are part of the design:
lane 8 (legacy momentum ensemble) must LOSE or the harness is wrong; lane 9
(deterministic random side) is the venue's cost baseline every real lane
must beat.

Selected via env ``HERMES_STRATEGY_VARIANT`` (default "baseline"). This
module is pure logic — the only wiring points are in hermes/mispricing.py
(q source, σ estimator, spot source, entry gate). The loop engine, executor,
and frozen gates are untouched.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LaneSpec:
    name: str
    description: str
    q_mode: str = "barrier"          # barrier | legacy_ensemble | random
    sigma_kind: str = "realized"     # realized | garch | market_implied
    spot_source: str = "cex"         # cex | chainlink
    min_side_price: float = 0.0      # favorite filter (price of model's side)
    max_side_price: float = 1.0      # longshot filter
    max_seconds_remaining: float = 1e9   # late-window filter
    min_liquidity_usd: float = 0.0   # depth filter


LANES: dict[str, LaneSpec] = {
    s.name: s
    for s in (
        LaneSpec("baseline", "barrier q, realized σ, no extra gates (control)"),
        # NOTE: lane02 runs the SAME "baseline" variant with HERMES_PURE_MODE
        # unset — the full-autonomy twin of pure lane01 (B1 autonomy A/B).
        # The old chainlink_ref variant is gone with the paid-oracle pivot.
        LaneSpec("favorite_only", "barrier gaps, model side priced >= 0.60",
                 min_side_price=0.60),
        LaneSpec("longshot_only", "barrier gaps, model side priced <= 0.40",
                 max_side_price=0.40),
        LaneSpec("late_window", "barrier gaps, last 5 min of the 15m window only",
                 max_seconds_remaining=300.0),
        LaneSpec("garch_sigma", "barrier q with GARCH(1,1) σ",
                 sigma_kind="garch"),
        LaneSpec("market_sigma_gap", "market-implied σ → pure spot-freshness gaps",
                 sigma_kind="market_implied"),
        LaneSpec("legacy_ensemble", "old momentum ensemble (negative control)",
                 q_mode="legacy_ensemble"),
        LaneSpec("random_null", "deterministic random side (null control)",
                 q_mode="random"),
        LaneSpec("depth_aware", "barrier gaps only into real book depth",
                 min_liquidity_usd=2000.0),
    )
}

ENV_VAR = "HERMES_STRATEGY_VARIANT"


def active_spec() -> LaneSpec:
    name = os.environ.get(ENV_VAR, "baseline").strip().lower() or "baseline"
    spec = LANES.get(name)
    if spec is None:
        # Unknown value → safest behavior is the control, loudly named.
        return LaneSpec(f"unknown({name})->baseline", LANES["baseline"].description)
    return spec


def random_q_for(slug: str, p_market: float, *, gap: float = 0.22) -> float:
    """Deterministic 'random' q for the null lane.

    Side from a stable hash of the slug (reproducible across restarts and
    lanes), pushed a fixed gap from the market so the normal entry gates can
    fire. Carries ZERO information about the outcome by construction.
    """
    h = int(hashlib.sha1(slug.encode()).hexdigest()[:8], 16)
    up = (h % 2) == 0
    q = p_market + gap if up else p_market - gap
    return float(min(0.95, max(0.05, q)))


def entry_allows(
    *,
    side_price: float,
    seconds_remaining: float,
    liquidity_usd: float,
    spec: Optional[LaneSpec] = None,
) -> tuple[bool, str]:
    """Lane entry gate on TOP of the normal (frozen) gates — never looser."""
    s = spec or active_spec()
    if side_price < s.min_side_price:
        return False, f"lane_gate:side_price={side_price:.2f}<{s.min_side_price:.2f}"
    if side_price > s.max_side_price:
        return False, f"lane_gate:side_price={side_price:.2f}>{s.max_side_price:.2f}"
    if seconds_remaining > s.max_seconds_remaining:
        return False, (
            f"lane_gate:too_early rem={seconds_remaining:.0f}s"
            f">{s.max_seconds_remaining:.0f}s"
        )
    if liquidity_usd < s.min_liquidity_usd:
        return False, f"lane_gate:thin_book liq={liquidity_usd:.0f}<{s.min_liquidity_usd:.0f}"
    return True, "lane_gate:ok"
