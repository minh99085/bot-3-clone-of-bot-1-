"""Asset resolution — SOL/ETH must not fall back to BTC."""

from __future__ import annotations

from connectors.hybrid_data import HybridDataService, detect_asset
from hermes.market_scope import resolve_asset
from hermes.models import MarketCandidate, Regime


def test_resolve_asset_from_sol_slug():
    slug = "sol-updown-5m-1784155800"
    assert resolve_asset(slug) == "SOL"
    assert resolve_asset(slug, meta={"cex_asset": "BTC", "asset": "BTC"}) == "SOL"


def test_detect_asset_sol_and_hybrid_preserves_asset():
    slug = "sol-updown-5m-1784155800"
    assert detect_asset(slug, "Solana Up or Down") == "SOL"

    cand = MarketCandidate(
        market_id="2928816",
        slug=slug,
        question="Solana Up or Down - July 15, 7:25PM-7:30PM ET",
        yes_price=0.55,
        no_price=0.45,
        regime=Regime.LOW_VOL,
        hourly_bucket=23,
        timeframe="5m",
        raw={
            "source": "polymarket_gamma",
            "asset": "SOL",
            "timeframe": "5m",
            "scoped_series": "sol_updown_5m",
            "yes_token_id": "tok_yes",
        },
    )
    svc = HybridDataService()
    out = svc.apply_to_candidate(cand)
    assert out.raw.get("asset") == "SOL"


def test_resolve_asset_eth_slug():
    assert resolve_asset("eth-updown-5m-1784157900") == "ETH"
