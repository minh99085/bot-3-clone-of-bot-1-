"""Frozen production gate protection — autonomy may NEVER loosen these.

Any optimizer / promoter that proposes a config must pass through
``assert_mutable_only`` before write. Attempts to change freeze keys raise.
"""

from __future__ import annotations

from typing import Any, Mapping

from models.config import STRICT_REAL_FREEZE

# Keys autonomy is allowed to mutate (soft knobs only)
MUTABLE_KEYS: frozenset[str] = frozenset(
    {
        "swarm_weight",
        "market_blend",
        "tf_weights",
        "tf_windows",
        "kalman_process_var",
        "kalman_measure_var",
        "garch_alpha",
        "garch_beta",
        "max_conviction_boost",
        "size_multiplier",
        "soft_kappa_scale",  # multiplies κ but never above kappa_base
        "explore_rate",
        "mchb_temperature",
        "cbpf_dirichlet_strength",
        "regime_weights",
    }
)

FROZEN_KEYS: frozenset[str] = frozenset(STRICT_REAL_FREEZE.keys()) | frozenset(
    {
        "mode",
        "min_edge",
        "min_conviction",
        "min_conviction_guard",
        "extreme_q_high",
        "extreme_q_low",
        "extreme_anchor",
        "extreme_p_high",
        "extreme_p_low",
        "kappa_base",
        "kappa_guard",
        "max_single_market_pct",
        "risk_budget",
        "dd_guard_pct",
        "max_drawdown_hard_pct",
        "rolling_wr_floor",
        "paper_only",
    }
)

# Live WR below this → auto-rollback of last promotion
ROLLBACK_WR_FLOOR = 0.78
# Shadow paper trades required before promote
SHADOW_PROMOTE_TRADES = 100
# Target self-report after enough resolved trades
TARGET_WR = 0.80
TARGET_RESOLVED_FOR_REPORT = 200


def assert_mutable_only(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Return only mutable keys; raise if any frozen key is present with a change."""
    out: dict[str, Any] = {}
    for k, v in proposal.items():
        if k in FROZEN_KEYS:
            raise PermissionError(
                f"autonomy freeze violation: cannot mutate frozen key {k!r}"
            )
        if k in MUTABLE_KEYS:
            out[k] = v
        # Unknown keys ignored (forward-compat)
    return out


def soft_kappa(kappa_base: float, soft_scale: float) -> float:
    """Scale κ down or keep — never raise above frozen kappa_base."""
    scale = float(max(0.05, min(1.0, soft_scale)))
    return float(kappa_base) * scale
