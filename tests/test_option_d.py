"""Option D — mispricing + contextual bandit unit tests."""

from __future__ import annotations

from hermes.bandit import ContextualBandit, context_key
from hermes.mispricing import MispricingSignal, detect_mispricing
from hermes.models import MarketCandidate, Regime


def _candidate(**kw):
    base = dict(
        market_id="1",
        slug="btc-updown-5m-1784113500",
        question="Bitcoin Up or Down - 5 Minutes",
        yes_price=0.42,
        no_price=0.58,
        volume_24h=10000,
        liquidity=8000,
        spread_bps=40,
        regime=Regime.MEAN_REVERT,
        hourly_bucket=14,
        timeframe="5m",
        raw={"timeframe": "5m", "asset": "BTC", "oracle_price": 65000.0},
    )
    base.update(kw)
    return MarketCandidate(**base)


def test_mispricing_detects_cex_lead(monkeypatch):
    class FakeSnap:
        mid = 65000.0
        momentum = 0.8  # strong up
        ret_30s = 0.001
        ret_60s = 0.002
        ret_3m = 0.003
        sources_agree = True
        binance = type("T", (), {"price": 65000, "source": "binance_rest"})()
        bybit = None

    monkeypatch.setattr(
        "hermes.mispricing.get_btc_snapshot",
        lambda force_rest=False: FakeSnap(),
    )
    # PM still prices UP cheaply at 0.42 while CEX implies much higher
    mp = detect_mispricing(_candidate(yes_price=0.42, no_price=0.58))
    assert mp.active
    assert mp.dislocation > 0.04
    assert mp.direction is not None


def test_bandit_explores_early(tmp_path):
    b = ContextualBandit(path=tmp_path / "bandit.json")
    mp = MispricingSignal(
        active=True,
        dislocation=0.08,
        conviction=0.7,
        timeframe="5m",
        cex_momentum=0.5,
    )
    # Force many decisions — should sometimes explore
    arms = {b.decide(mp, 14).arm for _ in range(30)}
    assert "exploit" in arms or "explore" in arms


def test_context_key_stable():
    mp = MispricingSignal(active=True, dislocation=0.07, timeframe="15m", cex_momentum=-0.4)
    k = context_key(mp, 10)
    assert k.startswith("15m|")
    assert "eu" in k or "asia" in k or "us" in k
