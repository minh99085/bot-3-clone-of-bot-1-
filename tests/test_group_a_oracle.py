"""Group A — oracle settlement authority (A1), latency probe (A2), barrier
eval on oracle-resolved windows (A3). All network mocked; real runs are on
the VPS via the scripts."""

from __future__ import annotations

import time

import pytest

import connectors.chainlink as cl


# --- A1: Chainlink is the ONLY crypto price authority, hard-fail closed ------

def test_price_at_hard_fails_without_creds(monkeypatch):
    monkeypatch.delenv("CHAINLINK_API_KEY", raising=False)
    monkeypatch.delenv("CHAINLINK_API_SECRET", raising=False)
    cl._ORACLE_CLIENT = None
    with pytest.raises(cl.OracleUnavailable):
        cl.oracle_price_at("BTC", 1_784_600_000)
    assert cl.oracle_enabled() is False


def test_agg_tier_enables_oracle_without_creds(monkeypatch):
    """Option 2: the free aggregator tier lets crypto lanes trade, no creds."""
    monkeypatch.delenv("CHAINLINK_API_KEY", raising=False)
    monkeypatch.delenv("CHAINLINK_API_SECRET", raising=False)
    cl._ORACLE_CLIENT = None
    # Off by default → still hard-fail closed.
    monkeypatch.delenv("HERMES_ORACLE_ALLOW_AGG", raising=False)
    assert cl.oracle_streams_enabled() is False
    assert cl.oracle_agg_allowed() is False
    assert cl.oracle_enabled() is False
    # Opt in → oracle_enabled() true so the crypto gate passes.
    monkeypatch.setenv("HERMES_ORACLE_ALLOW_AGG", "1")
    assert cl.oracle_agg_allowed() is True
    assert cl.oracle_streams_enabled() is False   # still no exact tier
    assert cl.oracle_enabled() is True


def test_price_at_uses_aggregator_when_agg_tier_and_no_creds(monkeypatch):
    monkeypatch.delenv("CHAINLINK_API_KEY", raising=False)
    monkeypatch.delenv("CHAINLINK_API_SECRET", raising=False)
    monkeypatch.setenv("HERMES_ORACLE_ALLOW_AGG", "1")
    cl._ORACLE_CLIENT = None
    monkeypatch.setattr(cl.ChainlinkClient, "agg_price_at",
                        lambda self, a, ts, **k: 63_500.0)
    assert cl.oracle_price_at("BTC", 1_784_600_000) == pytest.approx(63_500.0)


def test_price_at_agg_tier_raises_when_no_round(monkeypatch):
    """Aggregator with no round in effect → OracleUnavailable → caller SKIPS."""
    monkeypatch.delenv("CHAINLINK_API_KEY", raising=False)
    monkeypatch.delenv("CHAINLINK_API_SECRET", raising=False)
    monkeypatch.setenv("HERMES_ORACLE_ALLOW_AGG", "1")
    cl._ORACLE_CLIENT = None
    monkeypatch.setattr(cl.ChainlinkClient, "agg_price_at", lambda self, a, ts, **k: 0.0)
    with pytest.raises(cl.OracleUnavailable):
        cl.oracle_price_at("BTC", 1_784_600_000)


def test_streams_preferred_over_agg_when_both_available(monkeypatch):
    monkeypatch.setenv("HERMES_ORACLE_ALLOW_AGG", "1")
    monkeypatch.setenv("CHAINLINK_API_KEY", "k")
    monkeypatch.setenv("CHAINLINK_API_SECRET", "s")
    cl._ORACLE_CLIENT = None
    monkeypatch.setattr(cl.ChainlinkClient, "price_at", lambda self, a, ts: 64_000.0)
    monkeypatch.setattr(cl.ChainlinkClient, "agg_price_at",
                        lambda self, a, ts, **k: 1.0)  # must NOT be used
    assert cl.oracle_price_at("BTC", 1_784_600_000) == pytest.approx(64_000.0)


def test_price_at_uses_streams_report_when_credentialed(monkeypatch):
    client = cl.ChainlinkClient(api_key="k", api_secret="s")
    seen = {}

    def fake_report_at(feed_id, ts):
        seen["feed"] = feed_id
        seen["ts"] = ts
        return {"report": {"benchmarkPrice": str(64_123 * 10**18), "observationsTimestamp": ts}}

    monkeypatch.setattr(client, "get_report_at", fake_report_at)
    px = client.price_at("BTC", 1_784_600_000)
    assert px == pytest.approx(64_123.0)
    assert seen["feed"] == cl.FEED_BTC_USD and seen["ts"] == 1_784_600_000


def test_streams_failure_raises_oracle_unavailable(monkeypatch):
    client = cl.ChainlinkClient(api_key="k", api_secret="s")
    monkeypatch.setattr(client, "get_report_at",
                        lambda f, t: (_ for _ in ()).throw(RuntimeError("500")))
    with pytest.raises(cl.OracleUnavailable):
        client.price_at("BTC", 1)


def test_feeds_configured():
    feeds = cl.assert_feeds_configured()
    assert feeds["BTC"].startswith("0x") and feeds["ETH"].startswith("0x")


def test_settlement_routes_crypto_to_oracle(monkeypatch):
    import hermes.settlement_fast as stl

    calls = []
    monkeypatch.setattr(cl, "oracle_price_at", lambda a, ts: (calls.append((a, ts)) or 64_000.0))
    assert stl._open_price_at("BTC", 111) == 64_000.0
    assert stl._close_price_at("BTC", 222) == 64_000.0
    assert calls == [("BTC", 111), ("BTC", 222)]


def test_settlement_skips_when_oracle_unavailable(monkeypatch):
    import hermes.settlement_fast as stl

    def boom(a, ts):
        raise cl.OracleUnavailable("no creds")

    monkeypatch.setattr(cl, "oracle_price_at", boom)
    # returns 0.0 → caller SKIPS (never CEX fallback for crypto)
    assert stl._open_price_at("BTC", 111) == 0.0
    assert stl._close_price_at("ETH", 222) == 0.0


def test_detect_mispricing_hard_fails_without_oracle(monkeypatch):
    import hermes.mispricing as mp
    from hermes.models import MarketCandidate

    monkeypatch.setenv("HERMES_REQUIRE_ORACLE", "1")
    monkeypatch.setattr(cl, "oracle_enabled", lambda: False)
    cand = MarketCandidate(
        market_id="m", slug="btc-updown-15m-1784601000", question="BTC up/down",
        yes_price=0.6, no_price=0.4, hourly_bucket=12,
    )
    out = mp.detect_mispricing(cand)
    assert out.active is False
    assert out.reason == "oracle_required_unavailable"


# --- A2: latency probe classification ---------------------------------------

def test_stale_edge_when_pm_fresher_and_agrees():
    from hermes.latency_probe import classify

    rec = classify({
        "slug": "s", "asset": "BTC", "decision_ts": 1000.0,
        "oracle_ts": 990.0, "cex_ts": 991.0,
        "pm_updated_ts": 995.0,           # PM updated AFTER our oracle tick
        "pm_implied_up": 0.72, "model_q": 0.70,  # PM already on our side
        "dislocation": 0.02, "oracle_spot": 64000, "cex_mid": 64010,
    })
    assert rec.pm_fresher_than_oracle is True
    assert rec.pm_agrees_direction is True
    assert rec.stale_edge is True


def test_fresh_edge_when_pm_stale_or_opposed():
    from hermes.latency_probe import classify

    # PM updated BEFORE our tick and sits opposite our q → capturable
    rec = classify({
        "slug": "s", "asset": "BTC", "decision_ts": 1000.0,
        "oracle_ts": 998.0, "cex_ts": 998.0, "pm_updated_ts": 980.0,
        "pm_implied_up": 0.55, "model_q": 0.35, "dislocation": 0.20,
        "oracle_spot": 64000, "cex_mid": 64000,
    })
    assert rec.stale_edge is False


def test_stale_edge_rate_aggregate_and_verdict():
    from hermes.latency_probe import stale_edge_rate

    rows = [{"dislocation": 0.2, "stale_edge": True, "pm_agrees_direction": True}] * 7 + \
           [{"dislocation": 0.2, "stale_edge": False, "pm_agrees_direction": False}] * 3
    agg = stale_edge_rate(rows, min_dislocation=0.05)
    assert agg["n"] == 10
    assert agg["stale_edge_rate"] == pytest.approx(0.7)
    assert "NO CAPTURABLE EDGE" in agg["verdict"]


# --- A1 cross-check: Polymarket RTDS ----------------------------------------

def test_rtds_parse_and_cross_check():
    from connectors.polymarket_rtds import cross_check, parse_frame, subscribe_message

    assert "btc/usd" in subscribe_message("BTC")
    tick = parse_frame('{"payload": {"symbol": "btc/usd", "price": 64000.0, "timestamp": 1784600000}}')
    assert tick is not None and tick.price == 64000.0 and tick.symbol == "btc/usd"
    assert parse_frame('{"payload": {"symbol": "btc/usd"}}') is None  # no price
    assert cross_check(64000.0, 64005.0, tol_bps=15)["ok"] is True
    assert cross_check(64000.0, 64400.0, tol_bps=15)["ok"] is False


# --- A3: barrier eval resolves outcome on the oracle close ------------------

def test_barrier_eval_uses_close_price_fn_for_outcome():
    from backtest.barrier_eval import evaluate_barrier
    from backtest.paper_ledger import RealTrade

    # entry_cex says one thing; the ORACLE close decides the outcome.
    trades = []
    for i in range(40):
        up = i % 2 == 0
        trades.append(RealTrade(
            slug=f"btc-updown-15m-{1784600000 + i*900}", asset="btc", timeframe="15m",
            window_ts=1784600000 + i*900, settled_at="t", direction="UP",
            p_side=0.5, won=True, pnl_usd=0.0, size_usd=100.0,
            entry_cex=64000.0 * (1.004 if up else 0.996),
            exit_cex=1.0,  # deliberately wrong — must be IGNORED when close_fn given
        ))

    def oracle_open(asset, ts):
        return 64000.0

    def oracle_close(asset, ts):
        # up windows close above open, down below — the REAL resolution
        idx = (ts - 1784600000 - 900) // 900
        return 64000.0 * (1.005 if idx % 2 == 0 else 0.995)

    rep = evaluate_barrier(trades, open_price_fn=oracle_open, close_price_fn=oracle_close)
    assert rep.n_evaluated == 40  # exit_cex=1.0 did NOT exclude them
    assert rep.market_brier is not None and rep.barrier_brier is not None
