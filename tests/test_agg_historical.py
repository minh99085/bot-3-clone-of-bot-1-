"""Free on-chain AggregatorV3 historical lookup (A3 preliminary, no creds)."""

from __future__ import annotations

import pytest

import connectors.chainlink as cl


def _fake_rpc_factory(rounds: dict[int, tuple[float, int]], phase: int, latest_agg: int):
    """rounds: agg_round_id -> (price, updatedAt). Mimics eth_call responses."""
    def fake(self, to, data_hex):
        body = data_hex[2:] if data_hex.startswith("0x") else data_hex
        selector = "0x" + body[:8]
        if selector == cl._LATEST_ROUND_SELECTOR:
            return f"{(phase << 64) | latest_agg:064x}"
        # getRoundData(uint80): selector + 32-byte roundId
        rid = int(body[8:8 + 64], 16)
        agg = rid & cl._MASK64
        if agg not in rounds:
            raise RuntimeError("no data present")
        price, updated = rounds[agg]
        w = lambda v: f"{int(v):064x}"
        return w(rid) + w(int(price * 1e8)) + w(0) + w(updated) + w(rid)
    return fake


def test_agg_price_at_binary_search(monkeypatch):
    rounds = {i: (60000.0 + i * 100, i * 400) for i in range(1, 9)}  # updated = id*400
    monkeypatch.setattr(cl.ChainlinkClient, "_rpc_eth_call",
                        _fake_rpc_factory(rounds, phase=3, latest_agg=8))
    c = cl.ChainlinkClient()
    # latest round with updatedAt <= ts
    assert c.agg_price_at("BTC", 2100) == pytest.approx(60500.0)  # round 5 (2000)
    assert c.agg_price_at("BTC", 2800) == pytest.approx(60700.0)  # round 7 (2800)
    assert c.agg_price_at("BTC", 3200) == pytest.approx(60800.0)  # round 8 (3200)
    assert c.agg_price_at("BTC", 50) == 0.0                       # before round 1


def test_oracle_agg_price_at_swallows_errors(monkeypatch):
    def boom(self, to, data_hex):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(cl.ChainlinkClient, "_rpc_eth_call", boom)
    cl._ORACLE_CLIENT = None
    assert cl.oracle_agg_price_at("BTC", 1_784_600_000) == 0.0  # → caller excludes


def test_eval_excludes_same_round_flat_windows():
    from backtest.barrier_eval import evaluate_barrier
    from backtest.paper_ledger import RealTrade

    trades = []
    for i in range(20):
        trades.append(RealTrade(
            slug=f"btc-updown-15m-{1784600000 + i*900}", asset="btc", timeframe="15m",
            window_ts=1784600000 + i*900, settled_at="t", direction="UP",
            p_side=0.5, won=True, pnl_usd=0.0, size_usd=100.0,
            entry_cex=64000.0, exit_cex=64000.0,
        ))
    # Coarse oracle: even windows moved (distinct open/close), odd windows flat
    def openp(a, ts):
        return 64000.0
    def closep(a, ts):
        idx = (ts - 1784600000 - 900) // 900
        return 64100.0 if idx % 2 == 0 else 64000.0  # odd → == strike → excluded

    rep = evaluate_barrier(
        trades, open_price_fn=openp, close_price_fn=closep, exclude_equal_close=True
    )
    # ~half excluded as indeterminate same-round windows
    assert rep.n_excluded >= 8
    assert rep.n_evaluated <= 12
