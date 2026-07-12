"""PRISM Phase 6 — cross-asset lead-lag prior transfer (PAPER ONLY).

BTC leads the alt complex. A confident leader posterior (e.g. BTC strongly up) transfers a decayed
prior nudge onto a follower's belief before its own signals update it. The transfer decays with the
lead-lag horizon and the leader's distance from 0.5 (conviction). Observe-only nudge — it adjusts a
belief prior, never a fill. PAPER ONLY.
"""

from __future__ import annotations

import math
from typing import Optional

# Typical lead-lag (seconds) of each asset behind BTC's move.
LEAD_LAG_SEC: dict[str, float] = {
    "btc": 0.0,
    "eth": 5.0,
    "sol": 15.0,
    "bnb": 20.0,
    "doge": 30.0,
    "xrp": 45.0,
}

# Max prior nudge (in probability points) a fully-convicted leader can transfer to a follower.
_MAX_TRANSFER = 0.08

# Decay scale (seconds) for the lead-lag horizon.
_LAG_DECAY_S = 60.0


def _norm_asset(a: Optional[str]) -> str:
    return str(a or "").strip().lower().split("_")[0]


def transfer_posterior(leader_asset: str, follower_asset: str, leader_p: float,
                       *, lag_decay_s: float = _LAG_DECAY_S,
                       max_transfer: float = _MAX_TRANSFER) -> float:
    """Signed prior nudge (probability points) transferred from leader to follower.

    Zero when the follower is the leader, when the leader is at 0.5 (no conviction), or when the
    lead-lag horizon is so long the signal has decayed away.
    """
    lead = _norm_asset(leader_asset)
    follow = _norm_asset(follower_asset)
    if not lead or not follow or lead == follow:
        return 0.0
    try:
        p = float(leader_p)
    except (TypeError, ValueError):
        return 0.0
    conviction = max(-1.0, min(1.0, (p - 0.5) * 2.0))     # [-1, 1]
    lag = LEAD_LAG_SEC.get(follow, 30.0)
    decay = math.exp(-lag / max(1.0, lag_decay_s))         # (0, 1]
    return conviction * decay * max_transfer


def apply_cross_asset_prior(prior_p: float, leaders: dict, follower_asset: str,
                            *, lag_decay_s: float = _LAG_DECAY_S,
                            max_transfer: float = _MAX_TRANSFER) -> float:
    """Adjust ``prior_p`` for ``follower_asset`` by the summed decayed transfer from ``leaders``.

    ``leaders`` maps asset -> leader posterior P(up). Returns a clamped probability in (0, 1).
    """
    try:
        p = float(prior_p)
    except (TypeError, ValueError):
        return 0.5
    total = 0.0
    for asset, leader_p in (leaders or {}).items():
        total += transfer_posterior(asset, follower_asset, leader_p,
                                    lag_decay_s=lag_decay_s, max_transfer=max_transfer)
    return max(1e-4, min(1.0 - 1e-4, p + total))
