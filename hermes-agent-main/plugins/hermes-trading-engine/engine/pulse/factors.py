"""BTC-pulse factor / context model (OBSERVE-ONLY) — explains WHY edge may exist.

Computes small, auditable Hermes-native factors per candidate and a blended
``edge_quality_score`` (0..1) with human-readable ``reason_codes``. Factors:
CEX momentum, CEX volatility, Polymarket stale-price, orderbook imbalance, spread/liquidity,
time-to-resolution, crowded-side, settlement-boundary risk, and (optional) Grok/news context.

OBSERVE-ONLY: logged + bucketed in the report; never trades, sizes, or vetoes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def edge_quality_bucket(score: Optional[float]) -> str:
    if score is None:
        return "na"
    if score < 0.34:
        return "low"
    if score < 0.67:
        return "medium"
    return "high"


@dataclass
class FactorSnapshot:
    observe_only: bool = True
    cex_momentum: Optional[float] = None
    cex_volatility: Optional[float] = None
    polymarket_stale_factor: Optional[float] = None
    orderbook_imbalance: Optional[float] = None
    spread_liquidity_factor: Optional[float] = None
    time_to_resolution_factor: Optional[float] = None
    crowded_side_factor: Optional[float] = None
    settlement_boundary_risk: Optional[float] = None
    grok_context: Optional[str] = None
    edge_quality_score: float = 0.0
    edge_quality_bucket: str = "na"
    reason_codes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        def r(x):
            return round(x, 4) if isinstance(x, (int, float)) else x
        return {"observe_only": True, "cex_momentum": r(self.cex_momentum),
                "cex_volatility": r(self.cex_volatility),
                "polymarket_stale_factor": r(self.polymarket_stale_factor),
                "orderbook_imbalance": r(self.orderbook_imbalance),
                "spread_liquidity_factor": r(self.spread_liquidity_factor),
                "time_to_resolution_factor": r(self.time_to_resolution_factor),
                "crowded_side_factor": r(self.crowded_side_factor),
                "settlement_boundary_risk": r(self.settlement_boundary_risk),
                "grok_context": self.grok_context,
                "edge_quality_score": r(self.edge_quality_score),
                "edge_quality_bucket": self.edge_quality_bucket,
                "reason_codes": list(self.reason_codes)}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_factors(*, poly_yes: Optional[float], spread: Optional[float],
                    ask_depth_usd: Optional[float], bid_depth_usd: Optional[float],
                    ttc_s: Optional[float], signal: Optional[dict] = None,
                    divergence: Optional[float] = None, overlay_regime: Optional[str] = None,
                    min_depth_usd: float = 50.0, max_spread: float = 0.06,
                    settlement_boundary_s: float = 30.0) -> FactorSnapshot:
    """Pure factor computation. Missing inputs -> that factor is None (safe)."""
    f = FactorSnapshot(grok_context=overlay_regime)
    sig = signal or {}
    # CEX momentum/vol from the raw signal snapshot
    if sig.get("direction") in ("up", "down"):
        f.cex_momentum = (sig.get("strength") or 0.0) * (1 if sig["direction"] == "up" else -1)
    f.cex_volatility = sig.get("realized_vol")
    # Polymarket stale-price factor: how far the executable YES diverges from CEX-implied fair
    if divergence is not None:
        f.polymarket_stale_factor = _clamp01(abs(divergence) / 0.15)
    # orderbook imbalance
    if ask_depth_usd is not None and bid_depth_usd is not None \
            and (ask_depth_usd + bid_depth_usd) > 0:
        f.orderbook_imbalance = (bid_depth_usd - ask_depth_usd) / (bid_depth_usd + ask_depth_usd)
    # spread/liquidity factor: 1=excellent (tight + deep), 0=poor
    if spread is not None and ask_depth_usd is not None:
        tight = _clamp01(1.0 - (spread / max_spread))
        deep = _clamp01(ask_depth_usd / (min_depth_usd * 4.0))
        f.spread_liquidity_factor = round(0.5 * tight + 0.5 * deep, 4)
    # time-to-resolution factor: 1=plenty of time, 0=at the boundary
    if ttc_s is not None:
        f.time_to_resolution_factor = _clamp01(ttc_s / 300.0)
        f.settlement_boundary_risk = _clamp01(1.0 - (ttc_s / settlement_boundary_s)) \
            if ttc_s < settlement_boundary_s else 0.0
    # crowded-side factor: YES near 0/1 -> crowded/extreme (risky)
    if poly_yes is not None:
        f.crowded_side_factor = _clamp01((abs(poly_yes - 0.5) - 0.35) / 0.15) \
            if abs(poly_yes - 0.5) > 0.35 else 0.0

    # ---- blended edge_quality_score (0..1) + reason codes (observe-only) ----
    components = []
    codes = []
    if f.spread_liquidity_factor is not None:
        components.append(f.spread_liquidity_factor)
        codes.append("good_liquidity" if f.spread_liquidity_factor >= 0.6 else "thin_or_wide")
    if f.time_to_resolution_factor is not None:
        components.append(f.time_to_resolution_factor)
    if f.settlement_boundary_risk:
        components.append(1.0 - f.settlement_boundary_risk)
        codes.append("near_settlement_boundary")
    if f.crowded_side_factor:
        components.append(1.0 - f.crowded_side_factor)
        codes.append("crowded_side")
    if f.polymarket_stale_factor is not None:
        components.append(f.polymarket_stale_factor)   # divergence is the edge source here
        if f.polymarket_stale_factor >= 0.4:
            codes.append("polymarket_stale_divergence")
    if overlay_regime in ("event_risk", "elevated"):
        components.append(0.2)
        codes.append("grok_event_risk")
    f.edge_quality_score = round(sum(components) / len(components), 4) if components else 0.0
    f.edge_quality_bucket = edge_quality_bucket(f.edge_quality_score if components else None)
    f.reason_codes = codes
    return f


class FactorEngine:
    """Tracks factor coverage + PnL/calibration grouped by edge_quality bucket (observe-only)."""

    def __init__(self):
        self.coverage = {"snapshots": 0, "by_edge_quality_bucket": {}}
        self.by_bucket: dict = {}

    def observe(self, snap: FactorSnapshot) -> None:
        self.coverage["snapshots"] += 1
        b = snap.edge_quality_bucket
        self.coverage["by_edge_quality_bucket"][b] = \
            self.coverage["by_edge_quality_bucket"].get(b, 0) + 1

    def record_settled(self, *, bucket: Optional[str], pnl: float, won: bool) -> None:
        g = self.by_bucket.setdefault(bucket or "na", {"n": 0, "wins": 0, "pnl": 0.0})
        g["n"] += 1
        g["wins"] += int(bool(won))
        g["pnl"] = round(g["pnl"] + float(pnl), 6)

    def report(self) -> dict:
        summ = {k: {"n": g["n"], "win_rate": (round(g["wins"] / g["n"], 4) if g["n"] else None),
                    "pnl_usd": round(g["pnl"], 4)} for k, g in self.by_bucket.items()}
        return {"enabled": True, "observe_only": True, "affects_trading": False,
                **self.coverage, "pnl_by_edge_quality_bucket": summ}
