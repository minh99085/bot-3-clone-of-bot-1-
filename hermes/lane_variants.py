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
    q_mode: str = "barrier"          # barrier | barrier_drift | legacy_ensemble | random
    sigma_kind: str = "realized"     # realized | garch | market_implied
    spot_source: str = "cex"         # cex | chainlink
    min_side_price: float = 0.0      # favorite filter (price of model's side)
    max_side_price: float = 1.0      # longshot filter
    max_seconds_remaining: float = 1e9   # late-window filter
    min_liquidity_usd: float = 0.0   # depth filter
    require_momentum_agree: bool = False  # side must match intra-window drift


# Registry v2 (2026-07-23). The last-10h report was decisive: the driftless
# barrier's cheap-side fades ran 2/72 = 2.8% WR (fair ~20%, −4.3σ) and were
# −$1698 of a −$1272 fleet PnL — an ANTI-signal. The favorite side of those
# same windows won 97.2%. Proven losers are retired (longshot_only,
# late_window, market_sigma_gap, depth_aware, favorite_only, and
# legacy_ensemble whose negative-control duty is done at 0/16); the new
# family trades WITH intra-window drift instead of against it. Controls kept:
# random_null (lane09) and the pure/autonomy baseline pair (lane01/lane02 —
# the pre-registered H1, untouched).
LANES: dict[str, LaneSpec] = {
    s.name: s
    for s in (
        LaneSpec("baseline", "barrier q, realized σ, no extra gates (H1 control)"),
        # NOTE: lane02 runs the SAME "baseline" variant with HERMES_PURE_MODE
        # unset — the full-autonomy twin of pure lane01 (B1 autonomy A/B).
        LaneSpec("drift_barrier", "drift-adjusted barrier q, realized σ",
                 q_mode="barrier_drift"),
        LaneSpec("fav_cont_70", "buy the >=0.70 favorite when drift agrees, late half",
                 q_mode="barrier_drift", min_side_price=0.70,
                 max_seconds_remaining=450.0, require_momentum_agree=True),
        LaneSpec("fav_cont_80", "buy the >=0.80 favorite when drift agrees, late half",
                 q_mode="barrier_drift", min_side_price=0.80,
                 max_seconds_remaining=450.0, require_momentum_agree=True),
        LaneSpec("garch_sigma", "barrier q with GARCH(1,1) σ (mid book was +EV)",
                 sigma_kind="garch"),
        LaneSpec("drift_garch", "drift-adjusted barrier with GARCH(1,1) σ",
                 q_mode="barrier_drift", sigma_kind="garch"),
        LaneSpec("fav_cont_depth", "fav_cont_70 + real book depth only",
                 q_mode="barrier_drift", min_side_price=0.70,
                 max_seconds_remaining=450.0, require_momentum_agree=True,
                 min_liquidity_usd=2000.0),
        LaneSpec("random_null", "deterministic random side (null control)",
                 q_mode="random"),
        LaneSpec("fav_cont_open", "fav_cont_70 without the late-window gate",
                 q_mode="barrier_drift", min_side_price=0.70,
                 require_momentum_agree=True),
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


# Momentum dead-zone: |momentum| below this is "no confirmation" for the
# fav_cont lanes (a flat tape must not count as agreement in either direction).
MOMENTUM_AGREE_FLOOR = 0.05


def entry_allows(
    *,
    side_price: float,
    seconds_remaining: float,
    liquidity_usd: float,
    spec: Optional[LaneSpec] = None,
    momentum: float = 0.0,
    side_is_up: Optional[bool] = None,
) -> tuple[bool, str]:
    """Lane entry gate on TOP of the normal (frozen) gates — never looser."""
    s = spec or active_spec()

    # --- Emergency / risk pauses (ops) ---
    # Pause named variants without redeploying compose (comma-separated).
    paused = {
        x.strip().lower()
        for x in os.environ.get("HERMES_PAUSE_VARIANTS", "longshot_only").split(",")
        if x.strip()
    }
    if s.name in paused or s.name.startswith("longshot"):
        return False, f"lane_gate:variant_paused:{s.name}"

    # Global floor on ticket price for real (non-null) lanes.
    # Cheap longshots (side <= 0.25) had ~5% WR and large negative PnL live.
    # Override with HERMES_MIN_SIDE_PRICE=0 to disable.
    try:
        global_min = float(os.environ.get("HERMES_MIN_SIDE_PRICE", "0.25"))
    except ValueError:
        global_min = 0.25
    if s.q_mode not in ("random",) and global_min > 0 and side_price < global_min:
        return False, (
            f"lane_gate:cheap_ticket_blocked side={side_price:.2f}<{global_min:.2f}"
        )

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
    # Momentum agreement (fav_cont lanes): trade WITH the tape, never against
    # it, and never on a flat tape. The 2.8%-WR fade book was the against case.
    if s.require_momentum_agree:
        if side_is_up is None or abs(momentum) < MOMENTUM_AGREE_FLOOR:
            return False, f"lane_gate:no_momentum_confirmation mom={momentum:+.3f}"
        if (momentum > 0) != bool(side_is_up):
            return False, (
                f"lane_gate:momentum_opposes side={'UP' if side_is_up else 'DOWN'} "
                f"mom={momentum:+.3f}"
            )
    return True, "lane_gate:ok"
