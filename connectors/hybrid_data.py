"""Hybrid data layer — Polymarket CLOB + Chainlink oracles.

Gives discovery / signal / verifier a single snapshot per market with:
  - CLOB mid/spread/depth
  - Chainlink BTC/ETH ground-truth
  - oracle alignment score (manipulation / stale-data defense)
  - timeframe tag (5m / 15m / 1h) for HF crypto up-down markets
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from connectors.chainlink import ChainlinkClient, OraclePrice
from connectors.polymarket import OrderBookSnapshot, PolymarketClient, infer_timeframe
from hermes.models import MarketCandidate, Regime

logger = logging.getLogger(__name__)


@dataclass
class HybridSnapshot:
    market_id: str
    slug: str
    timeframe: str
    asset: Optional[str]  # BTC | ETH | None
    yes_price: float
    no_price: float
    spread_bps: float
    orderbook: Optional[OrderBookSnapshot] = None
    oracle: Optional[OraclePrice] = None
    oracle_alignment: float = 0.5  # [0,1] higher = PM agrees with oracle dynamics
    oracle_return_proxy: float = 0.0
    regime: Regime = Regime.UNKNOWN
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    meta: dict[str, Any] = field(default_factory=dict)


def detect_asset(
    slug: str,
    question: str = "",
    raw: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    from hermes.market_scope import parse_slug, resolve_asset

    sm = parse_slug(slug) if slug else None
    if sm:
        return sm.asset.upper()
    if raw:
        prior = raw.get("asset")
        if prior:
            au = str(prior).upper()
            if au in ("BTC", "ETH", "SOL"):
                return au
    resolved = resolve_asset(slug, meta=raw or {}, default="")
    return resolved if resolved in ("BTC", "ETH", "SOL") else None


def oracle_alignment_score(
    *,
    yes_price: float,
    oracle_return: float,
    timeframe: str,
) -> float:
    """Score how well Polymarket YES pricing aligns with Chainlink return.

    For up/down markets: YES ≈ P(up). Positive oracle return should lift YES.
    Misalignment (YES high while CL dumping) lowers score → verifier can reject.
    """
    # Implied up-prob vs oracle short-horizon move
    implied_up = yes_price
    # Map oracle return to expected up-prob tilt
    scale = {"5m": 80.0, "15m": 50.0, "1h": 25.0}.get(timeframe, 30.0)
    expected_up = 0.5 + max(-0.45, min(0.45, oracle_return * scale))
    err = abs(implied_up - expected_up)
    # 0 err → 1.0; 0.25 err → ~0.0
    score = max(0.0, 1.0 - err / 0.25)
    return float(score)


def regime_from_oracle(
    oracle_return: float,
    spread_bps: float,
    yes_price: float,
    timeframe: str,
) -> Regime:
    """Regime detection using Chainlink returns — stronger for 5m/15m."""
    thr = {"5m": 0.0015, "15m": 0.003, "1h": 0.008}.get(timeframe, 0.01)
    if spread_bps > 400:
        return Regime.HIGH_VOL
    if abs(oracle_return) >= thr * 4:
        return Regime.HIGH_VOL
    if oracle_return >= thr:
        return Regime.TRENDING_UP
    if oracle_return <= -thr:
        return Regime.TRENDING_DOWN
    if abs(oracle_return) < thr * 0.5 and 0.25 <= yes_price <= 0.75:
        return Regime.MEAN_REVERT
    return Regime.LOW_VOL if abs(oracle_return) < thr else Regime.MEAN_REVERT


class HybridDataService:
    def __init__(
        self,
        polymarket: Optional[PolymarketClient] = None,
        chainlink: Optional[ChainlinkClient] = None,
    ):
        self.pm = polymarket or PolymarketClient()
        self.cl = chainlink or ChainlinkClient()

    def enrich_candidate(self, candidate: MarketCandidate) -> HybridSnapshot:
        timeframe = (
            (candidate.raw or {}).get("timeframe")
            or infer_timeframe(candidate.slug, candidate.question)
        )
        raw_in = dict(candidate.raw or {})
        asset = detect_asset(candidate.slug, candidate.question, raw_in)
        if not asset:
            asset = str(raw_in.get("asset") or "").upper() or None
        oracle = None
        oracle_ret = 0.0
        if asset in ("BTC", "ETH"):
            try:
                # Warm cache then measure proxy return
                self.cl.get_price(asset)
                oracle_ret = self.cl.returns_proxy(asset)
                oracle = self.cl.get_price(asset)
            except Exception as exc:  # noqa: BLE001
                logger.debug("oracle enrich failed: %s", exc)
        elif asset == "SOL":
            try:
                from connectors.cex_realtime import get_asset_snapshot

                snap_cex = get_asset_snapshot("SOL", force_rest=True)
                oracle_ret = float(snap_cex.ret_60s or 0.0)
            except Exception as exc:  # noqa: BLE001
                logger.debug("SOL cex oracle enrich failed: %s", exc)

        book = None
        yes_token = (candidate.raw or {}).get("yes_token_id")
        spread = candidate.spread_bps
        yes = candidate.yes_price
        no = candidate.no_price
        if yes_token:
            try:
                book = self.pm.get_orderbook(str(yes_token))
                if book.mid is not None:
                    yes = book.mid
                    no = max(0.01, min(0.99, 1.0 - yes))
                if book.spread_bps:
                    spread = book.spread_bps
            except Exception as exc:  # noqa: BLE001
                logger.debug("orderbook enrich failed: %s", exc)

        align = (
            oracle_alignment_score(
                yes_price=yes, oracle_return=oracle_ret, timeframe=timeframe
            )
            if asset
            else 0.55
        )
        regime = (
            regime_from_oracle(oracle_ret, spread, yes, timeframe)
            if asset
            else candidate.regime
        )
        return HybridSnapshot(
            market_id=candidate.market_id,
            slug=candidate.slug,
            timeframe=timeframe,
            asset=asset,
            yes_price=yes,
            no_price=no,
            spread_bps=spread,
            orderbook=book,
            oracle=oracle,
            oracle_alignment=align,
            oracle_return_proxy=oracle_ret,
            regime=regime,
            meta={
                "oracle_source": oracle.source if oracle else None,
                "oracle_stale": oracle.stale if oracle else None,
                "book_source": book.source if book else None,
            },
        )

    def apply_to_candidate(self, candidate: MarketCandidate) -> MarketCandidate:
        snap = self.enrich_candidate(candidate)
        candidate.yes_price = snap.yes_price
        candidate.no_price = snap.no_price
        candidate.spread_bps = snap.spread_bps
        candidate.regime = snap.regime
        raw = dict(candidate.raw or {})
        prior_asset = str(raw.get("asset") or "").upper()
        asset_out = snap.asset or (
            prior_asset if prior_asset in ("BTC", "ETH", "SOL") else None
        )
        if not asset_out:
            from hermes.market_scope import resolve_asset

            asset_out = resolve_asset(candidate.slug, meta=raw)
        raw.update(
            {
                "timeframe": snap.timeframe,
                "asset": asset_out,
                "oracle_alignment": snap.oracle_alignment,
                "oracle_return_proxy": snap.oracle_return_proxy,
                "oracle_price": snap.oracle.price_usd if snap.oracle else None,
                "oracle_source": snap.oracle.source if snap.oracle else None,
                "oracle_stale": snap.oracle.stale if snap.oracle else None,
                "hybrid_ts": snap.ts.isoformat(),
            }
        )
        candidate.raw = raw
        if snap.timeframe and f"tf:{snap.timeframe}" not in candidate.tags:
            candidate.tags = list(candidate.tags) + [f"tf:{snap.timeframe}"]
        return candidate
