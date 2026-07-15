"""CEX↔Polymarket mispricing / lead-lag detection for BTC 5m/15m Up/Down.

Compares:
  - Binance (primary) short-horizon momentum
  - Polymarket implied P(UP) = yes_price
  - Chainlink as resolution-reference anchor

Outputs a directional bias + conviction for the signal / bandit layers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from connectors.cex_realtime import BtcSnapshot, get_btc_snapshot
from hermes.models import Direction, MarketCandidate

logger = logging.getLogger(__name__)

# Minimum absolute dislocation (prob points) to flag a setup
MIN_DISLOCATION = 0.04
STRONG_DISLOCATION = 0.10


@dataclass
class MispricingSignal:
    """Detected short-horizon dislocation between CEX action and PM odds."""

    active: bool = False
    direction: Optional[Direction] = None  # UP/DOWN for BTC updown markets
    dislocation: float = 0.0  # signed: + => CEX implies more UP than PM
    conviction: float = 0.0  # [0,1]
    cex_momentum: float = 0.0
    cex_mid: float = 0.0
    pm_implied_up: float = 0.5
    cex_implied_up: float = 0.5
    chainlink_price: Optional[float] = None
    chainlink_vs_cex_bps: float = 0.0
    sources_agree: bool = True
    timeframe: str = "5m"
    reason: str = ""
    features: dict[str, float] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_meta(self) -> dict[str, Any]:
        return {
            "mispricing_active": self.active,
            "mispricing_dislocation": round(self.dislocation, 5),
            "mispricing_conviction": round(self.conviction, 4),
            "cex_momentum": round(self.cex_momentum, 4),
            "cex_mid": self.cex_mid,
            "pm_implied_up": self.pm_implied_up,
            "cex_implied_up": round(self.cex_implied_up, 4),
            "chainlink_vs_cex_bps": round(self.chainlink_vs_cex_bps, 2),
            "mispricing_reason": self.reason,
            "entry_source": "mispricing" if self.active else "baseline",
        }


def _cex_implied_up(momentum: float, timeframe: str) -> float:
    """Map CEX momentum → rough P(UP) for the remaining window.

    Strong positive momentum → implied_up > 0.5.
    Scale is tighter for 5m than 15m (less time for mean-reversion).
    """
    scale = 0.35 if timeframe == "5m" else 0.28
    return max(0.05, min(0.95, 0.5 + momentum * scale))


def detect_mispricing(
    candidate: MarketCandidate,
    *,
    snapshot: Optional[BtcSnapshot] = None,
    chainlink_price: Optional[float] = None,
) -> MispricingSignal:
    """Core detector — safe to call every turn for scoped BTC up/down markets."""
    tf = candidate.timeframe or (candidate.raw or {}).get("timeframe") or "5m"
    pm_up = float(candidate.yes_price)
    snap = snapshot or get_btc_snapshot()

    out = MispricingSignal(
        cex_momentum=snap.momentum,
        cex_mid=snap.mid,
        pm_implied_up=pm_up,
        sources_agree=snap.sources_agree,
        timeframe=tf,
        chainlink_price=chainlink_price,
    )

    if snap.mid <= 0:
        out.reason = "no_cex_price"
        return out

    if chainlink_price and chainlink_price > 0:
        out.chainlink_vs_cex_bps = (snap.mid - chainlink_price) / chainlink_price * 10_000

    cex_up = _cex_implied_up(snap.momentum, tf)
    out.cex_implied_up = cex_up
    dislocation = cex_up - pm_up  # + means CEX says more UP than PM prices
    out.dislocation = dislocation

    # Features for bandit context
    out.features = {
        "dislocation": abs(dislocation),
        "dislocation_signed": dislocation,
        "momentum": snap.momentum,
        "ret_60s": snap.ret_60s,
        "ret_3m": snap.ret_3m,
        "pm_implied_up": pm_up,
        "oracle_gap_bps": abs(out.chainlink_vs_cex_bps),
        "sources_agree": 1.0 if snap.sources_agree else 0.0,
        "tf_5m": 1.0 if tf == "5m" else 0.0,
    }

    # Require sources roughly agree when Bybit present
    if snap.bybit and not snap.sources_agree and abs(dislocation) < STRONG_DISLOCATION:
        out.reason = "cex_sources_disagree"
        return out

    if abs(dislocation) < MIN_DISLOCATION:
        out.reason = f"dislocation|{dislocation:.3f}|<|{MIN_DISLOCATION}"
        return out

    # Direction: trade with CEX momentum (lead) when PM lags
    if dislocation > 0:
        direction = Direction.UP
    else:
        direction = Direction.DOWN

    # Conviction from magnitude + momentum alignment + oracle proximity
    mag = min(1.0, abs(dislocation) / STRONG_DISLOCATION)
    mom_align = 1.0 if (dislocation * snap.momentum) > 0 else 0.4
    oracle_ok = 1.0
    if abs(out.chainlink_vs_cex_bps) > 25:  # CEX far from Chainlink — caution
        oracle_ok = 0.6
    conviction = max(0.0, min(1.0, 0.55 * mag + 0.30 * mom_align + 0.15 * oracle_ok))

    out.active = True
    out.direction = direction
    out.conviction = conviction
    out.reason = (
        f"cex_lead dislocation={dislocation:+.3f} mom={snap.momentum:+.2f} "
        f"pm_up={pm_up:.3f} cex_up={cex_up:.3f}"
    )
    logger.info("mispricing %s: %s conv=%.2f", candidate.slug, out.reason, conviction)
    return out
