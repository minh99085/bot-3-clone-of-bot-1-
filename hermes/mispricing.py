"""CEX↔Polymarket mispricing / lead-lag detection for BTC 5m/15m Up/Down.

Compares:
  - Binance (primary) short-horizon momentum + advanced multi-signal ensemble
  - Polymarket implied P(UP) = yes_price
  - Chainlink as resolution-reference anchor

Outputs a directional bias + conviction for the signal / bandit layers.

Upgrade (Hermes v3): toy momentum→q is replaced by
``strategy.advanced_signals.ensemble_cex_implied_up`` when CEX history is
available. Fallback to the legacy map keeps overnight loops zero-config.
STRICT_REAL_FREEZE and live_real_q path are unchanged downstream.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from connectors.cex_realtime import (
    BtcSnapshot,
    get_asset_price_history,
    get_asset_snapshot,
    get_feed,
)
from hermes.models import Direction, MarketCandidate
from strategy.advanced_signals import ensemble_cex_implied_up, momentum_to_q

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
            **{
                k: v
                for k, v in self.features.items()
                if k.startswith(("hurst", "obi", "kalman", "garch", "slope", "ou_", "vamp", "ir", "advanced_"))
            },
        }


def _cex_implied_up(momentum: float, timeframe: str) -> float:
    """Legacy toy map — kept for backward-compatible unit tests / fallback."""
    return momentum_to_q(momentum, timeframe)


def _advanced_enabled() -> bool:
    """Env HERMES_ADVANCED_SIGNALS=0 disables ensemble (default on)."""
    return os.environ.get("HERMES_ADVANCED_SIGNALS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _load_advanced_cfg() -> dict[str, Any]:
    try:
        from models.config import load_enhanced_config

        cfg = load_enhanced_config()
        adv = getattr(cfg, "advanced", None)
        if adv is None:
            return {}
        if hasattr(adv, "model_dump"):
            return dict(adv.model_dump())
        return dict(adv) if isinstance(adv, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("advanced config load failed: %s", exc)
        return {}


def _fusion_weights_override(adv_cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge self-calibrated fusion weights when present (CBPF + legacy)."""
    try:
        from strategy.signal_calibration import load_fusion_overrides

        ov = load_fusion_overrides()
        if ov:
            if "swarm_weight" in ov:
                adv_cfg = {**adv_cfg, "swarm_weight": ov["swarm_weight"]}
            if "market_blend" in ov:
                adv_cfg = {**adv_cfg, "market_blend": ov["market_blend"]}
    except Exception as exc:  # noqa: BLE001
        logger.debug("fusion override load failed: %s", exc)
    # Prefer CBPF / autonomy mutable params when available
    try:
        from autonomy.orchestrator import load_autonomy_state

        st = load_autonomy_state()
        mp = st.mutable_params or {}
        if "swarm_weight" in mp:
            adv_cfg = {**adv_cfg, "swarm_weight": float(mp["swarm_weight"])}
        if "market_blend" in mp:
            adv_cfg = {**adv_cfg, "market_blend": float(mp["market_blend"])}
    except Exception as exc:  # noqa: BLE001
        logger.debug("autonomy fusion override failed: %s", exc)
    try:
        from autonomy.cbpf import get_cbpf

        export = get_cbpf().mutable_export()
        if export.get("swarm_weight") is not None and get_cbpf().n_updates >= 25:
            adv_cfg = {
                **adv_cfg,
                "swarm_weight": float(export["swarm_weight"]),
                "market_blend": float(export["market_blend"]),
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("cbpf override failed: %s", exc)
    return adv_cfg


def compute_cex_implied_up(
    *,
    momentum: float,
    timeframe: str,
    pm_implied_up: float,
    spot: float,
    asset: str = "BTC",
    seconds_to_resolution: float = 300.0,
    strike: Optional[float] = None,
) -> tuple[float, dict[str, float], dict[str, Any]]:
    """Return (q, features, meta) from advanced ensemble or toy fallback."""
    adv_cfg = _fusion_weights_override(_load_advanced_cfg())
    enabled = _advanced_enabled() and bool(adv_cfg.get("enabled", True))

    times, prices = get_asset_price_history(asset, max_points=240)
    bids = None
    asks = None
    if asset.upper() == "BTC":
        try:
            bid, ask, bsz, asz = get_feed().get_top_of_book()
            if bid and ask and bid > 0 and ask > 0:
                # Multi-level not on bookTicker — synthesize shallow ladder
                bids = [(bid, bsz), (bid * 0.9999, bsz), (bid * 0.9998, bsz)]
                asks = [(ask, asz), (ask * 1.0001, asz), (ask * 1.0002, asz)]
        except Exception as exc:  # noqa: BLE001
            logger.debug("top-of-book unavailable: %s", exc)

    result = ensemble_cex_implied_up(
        prices=prices,
        times=times,
        momentum=momentum,
        timeframe=timeframe,
        pm_implied_up=pm_implied_up,
        spot=spot,
        strike=strike,
        seconds_to_resolution=seconds_to_resolution,
        bids=bids,
        asks=asks,
        swarm_weight=float(adv_cfg.get("swarm_weight", 0.70)),
        market_blend=float(adv_cfg.get("market_blend", 0.30)),
        tf_windows=tuple(adv_cfg.get("tf_windows") or (30.0, 60.0, 120.0, 240.0)),
        tf_weights=tuple(adv_cfg.get("tf_weights") or (0.15, 0.20, 0.30, 0.35)),
        enabled=enabled,
    )

    features = dict(result.features)
    features["advanced_q"] = float(result.q)
    features["advanced_conviction_boost"] = float(result.conviction_boost)
    features["advanced_used_fallback"] = 1.0 if result.used_fallback else 0.0
    if result.regime:
        features["advanced_regime_mr"] = 1.0 if result.regime == "mean_reversion" else 0.0
        features["advanced_regime_mom"] = 1.0 if result.regime == "momentum" else 0.0

    meta: dict[str, Any] = {
        "model_q_source": (
            "advanced_ensemble" if not result.used_fallback else "momentum_fallback"
        ),
        "advanced_regime": result.regime,
        "advanced_components": dict(result.components),
        "advanced_reason": result.reason,
        **(result.meta or {}),
    }
    return float(result.q), features, meta


def detect_mispricing(
    candidate: MarketCandidate,
    *,
    snapshot: Optional[BtcSnapshot] = None,
    chainlink_price: Optional[float] = None,
) -> MispricingSignal:
    """Core detector — safe to call every turn for scoped BTC up/down markets."""
    tf = candidate.timeframe or (candidate.raw or {}).get("timeframe") or "5m"
    pm_up = float(candidate.yes_price)
    from hermes.market_scope import resolve_asset, window_remaining_seconds

    raw = candidate.raw or {}
    asset = resolve_asset(candidate.slug or "", meta=raw)
    snap = snapshot
    if snap is None:
        snap = get_asset_snapshot(asset)

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

    rem = window_remaining_seconds(candidate.slug) if candidate.slug else None
    if rem is not None:
        sec_res = max(30.0, float(rem))
    else:
        sec_res = 300.0 if tf == "5m" else 900.0

    strike = None
    try:
        if raw.get("strike") is not None:
            strike = float(raw["strike"])
        elif raw.get("price_to_beat") is not None:
            strike = float(raw["price_to_beat"])
    except (TypeError, ValueError):
        strike = None

    cex_up, adv_features, adv_meta = compute_cex_implied_up(
        momentum=snap.momentum,
        timeframe=tf,
        pm_implied_up=pm_up,
        spot=float(snap.mid),
        asset=asset,
        seconds_to_resolution=sec_res,
        strike=strike,
    )
    out.cex_implied_up = cex_up
    dislocation = cex_up - pm_up  # + means CEX says more UP than PM prices
    out.dislocation = dislocation

    # Features for bandit context + advanced diagnostics
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
        **{k: float(v) for k, v in adv_features.items() if isinstance(v, (int, float))},
    }
    # Stash non-float meta under features keys with advanced_ prefix strings via reason
    out.features["advanced_component_count"] = float(
        len(adv_meta.get("advanced_components") or {})
    )

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

    # Optional mild boost when advanced sub-signals agree (capped; never loosens gates)
    boost = float(adv_features.get("advanced_conviction_boost") or 0.0)
    if boost > 0 and not adv_features.get("advanced_used_fallback"):
        conviction = min(1.0, conviction + 0.5 * boost)

    # Strong disagreement between ensemble and simple momentum → log; keep selectivity
    toy_q = _cex_implied_up(snap.momentum, tf)
    if abs(cex_up - toy_q) > 0.12:
        logger.info(
            "advanced vs momentum disagree slug=%s ens=%.3f mom=%.3f regime=%s",
            candidate.slug,
            cex_up,
            toy_q,
            adv_meta.get("advanced_regime"),
        )

    out.active = True
    out.direction = direction
    out.conviction = conviction
    src = adv_meta.get("model_q_source", "unknown")
    out.reason = (
        f"cex_lead dislocation={dislocation:+.3f} mom={snap.momentum:+.2f} "
        f"pm_up={pm_up:.3f} cex_up={cex_up:.3f} src={src} "
        f"regime={adv_meta.get('advanced_regime', 'n/a')}"
    )
    logger.info("mispricing %s: %s conv=%.2f", candidate.slug, out.reason, conviction)
    return out
