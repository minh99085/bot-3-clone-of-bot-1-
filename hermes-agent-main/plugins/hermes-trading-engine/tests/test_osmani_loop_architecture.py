"""Tests for Osmani 2026 loop architecture (3 lanes + maker-checker + MEMORY.md)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from engine.pulse.loop_architecture.circuit_breaker import CircuitBreakerConfig, LoopCircuitBreaker
from engine.pulse.loop_architecture.lanes import SWEET_SPOT_MAX, SWEET_SPOT_MIN, DiscoveryLane
from engine.pulse.loop_architecture.maker_checker import (
    TradeEvaluator,
    TradeGenerator,
    TradeOpportunity,
)
from engine.pulse.loop_architecture.memory import LoopMemory
from engine.pulse.markets import OrderBook, PulseWindow


def _window(ask: float = 0.50) -> PulseWindow:
    book = OrderBook(
        best_bid=ask - 0.02,
        best_ask=ask,
        ask_depth_usd=5000.0,
        bid_depth_usd=5000.0,
        asks=[(ask, 10000.0)],
        bids=[(ask - 0.02, 10000.0)],
    )
    return PulseWindow(
        event_id="evt-1",
        market_id="m1",
        slug="btc-up-or-down-hourly-test",
        title="BTC hourly test",
        open_ts=1_000_000.0,
        close_ts=1_003_600.0,
        up_token_id="up",
        down_token_id="dn",
        series_slug="btc-up-or-down-hourly",
        window_seconds=3600,
        series_label="btc_1h",
        up_book=book,
        down_book=book,
    )


def test_discovery_blocks_early_hourly_entry():
    import queue
    from engine.pulse.hourly_entry_timing import HourlyEntryEvidence, LearnedHourlyEntryGate

    q = queue.Queue()
    w = _window(0.50)
    gate = LearnedHourlyEntryGate(enabled=True, min_seconds_since_open=900.0)
    evidence = HourlyEntryEvidence()

    def hourly_fn(window, now):
        return gate.evaluate(
            window_seconds=3600,
            seconds_since_open=window.seconds_since_open(now),
            evidence=evidence,
        )

    lane = DiscoveryLane(
        out_queue=q,
        windows_fn=lambda now: [w],
        fair_fn=lambda _w, _now: 0.55,
        min_edge=0.003,
        interval_s=60.0,
        hourly_entry_fn=hourly_fn,
    )
    lane._scan_once(1_000_100.0)
    assert lane.emitted == 0
    assert lane.hourly_entry_blocked == 1
    lane._scan_once(1_000_900.0)
    assert lane.emitted == 1
    assert lane.hourly_entry_blocked == 1


def test_memory_load_save_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        mem = LoopMemory(Path(td))
        mem.record_decision(event_id="e1", side="up", status="verified")
        mem.update_capital({"on_hand_capital_usd": 502.0})
        mem.save()
        mem2 = LoopMemory(Path(td))
        loaded = mem2.load()
        assert loaded["wake_count"] == 1
        assert mem2.path.exists()
        text = mem2.path.read_text(encoding="utf-8")
        assert "MEMORY.md" in text
        assert "e1" in text


def test_circuit_breaker_trips_on_capital_floor():
    br = LoopCircuitBreaker(cfg=CircuitBreakerConfig(enabled=True, min_on_hand_capital_usd=100.0))
    assert br.check_capital(50.0, 500.0) is False
    assert br.tripped is True


def test_discovery_emits_sweet_spot_opportunity():
    import queue
    q = queue.Queue()
    w = _window(0.50)

    def fair(_w, _now):
        return 0.55

    lane = DiscoveryLane(
        out_queue=q,
        windows_fn=lambda now: [w],
        fair_fn=fair,
        min_edge=0.003,
        interval_s=60.0,
    )
    lane._scan_once(1_000_100.0)
    assert lane.emitted == 1
    opp = q.get_nowait()
    assert SWEET_SPOT_MIN <= opp.ask_price <= SWEET_SPOT_MAX
    assert opp.edge >= 0.003


def test_evaluator_rejects_without_hydrate():
    gen = TradeGenerator()
    ev = TradeEvaluator(hydrate_fn=lambda s: (_ for _ in ()).throw(ValueError("no api")))
    opp = TradeOpportunity(
        opportunity_id="o1",
        event_id="e1",
        series_slug="btc-up-or-down-hourly",
        side="up",
        ask_price=0.50,
        fair_p=0.55,
        edge=0.05,
        size_usd=5.0,
        ttc_s=300.0,
        tick_size=0.01,
        discovered_at=1.0,
        window_snapshot={"event_id": "e1"},
    )
    prop = gen.propose(opp, worktree_id="wt1")
    verdict = ev.evaluate(prop, opp.window_snapshot)
    assert verdict.verified is False


def test_evaluator_accepts_positive_ev_after_hydrate():
    gen = TradeGenerator()
    w = _window(0.50)

    def hydrate(_snap):
        return w

    ev = TradeEvaluator(
        hydrate_fn=hydrate,
        min_ev_after_slippage=0.001,
        max_spread=0.10,
        min_entry_price=0.30,
    )
    opp = TradeOpportunity(
        opportunity_id="o1",
        event_id="e1",
        series_slug="btc-up-or-down-hourly",
        side="up",
        ask_price=0.50,
        fair_p=0.55,
        edge=0.05,
        size_usd=5.0,
        ttc_s=300.0,
        tick_size=0.01,
        discovered_at=1.0,
        window_snapshot={"event_id": "e1"},
    )
    prop = gen.propose(opp, worktree_id="wt1")
    verdict = ev.evaluate(prop, opp.window_snapshot)
    assert verdict.verified is True
    assert verdict.fill_price is not None


def test_directional_authority_osmani_when_enabled(tmp_path):
    from engine.pulse.engine import PulseEngine, PulseConfig
    from engine.pulse.markets import PulseWindow
    eng = PulseEngine(PulseConfig(data_dir=str(tmp_path), osmani_loop_enabled=True,
                                   directional_legacy_tick=False))
    assert eng.osmani_loop is not None
    assert eng._directional_trade_authority_osmani() is True
    eng2 = PulseEngine(PulseConfig(data_dir=str(tmp_path), osmani_loop_enabled=True,
                                    directional_legacy_tick=True))
    assert eng2._directional_trade_authority_osmani() is False
    lane_w = PulseWindow(
        event_id="lane", market_id="m", slug="btc-15", title="BTC 15m",
        open_ts=1.0, close_ts=901.0, up_token_id="u", down_token_id="d",
        directional_lane=True, series_label="btc_15m",
    )
    assert eng._directional_trade_authority_osmani(lane_w) is False


def test_osmani_fair_p_routes_eth_window_to_eth_oracle(tmp_path):
    """Regression: ETH hourly must not be priced off the BTC Chainlink feed."""
    from engine.pulse.engine import PulseEngine, PulseConfig
    from engine.pulse.markets import OrderBook, PulseWindow

    eng = PulseEngine(PulseConfig(
        data_dir=str(tmp_path),
        osmani_loop_enabled=True,
        directional_legacy_tick=False,
        directional_series_slugs=("btc-up-or-down-hourly", "eth-up-or-down-hourly"),
    ))
    eth_w = PulseWindow(
        event_id="eth-evt",
        market_id="m-eth",
        slug="eth-up-or-down-hourly-test",
        title="ETH hourly",
        open_ts=1_000_000.0,
        close_ts=1_003_600.0,
        up_token_id="u",
        down_token_id="d",
        series_slug="eth-up-or-down-hourly",
        series_label="eth_1h",
        window_seconds=3600,
        up_book=OrderBook(best_bid=0.48, best_ask=0.50, ask_depth_usd=5000,
                          bid_depth_usd=5000, asks=[(0.50, 10000)], bids=[(0.48, 10000)]),
        down_book=OrderBook(best_bid=0.48, best_ask=0.50, ask_depth_usd=5000,
                            bid_depth_usd=5000, asks=[(0.50, 10000)], bids=[(0.48, 10000)]),
    )
    assert eng._window_asset(eth_w) == "eth"
    # Stub ETH oracle with a known spot + open; leave BTC oracle empty for this event.
    class _Feed:
        def __init__(self):
            self.snapped = []
            self._price = 2000.0
            self._open = 1990.0

        def snapshot_open(self, eid, open_ts, now=None):
            self.snapped.append(eid)

        def current(self):
            return self._price

        def sigma_per_sec(self, now):
            return 1e-5

        def open_snapshot(self, eid):
            class S:
                price = 1990.0
            return S()

        def is_fresh(self, max_age, now):
            return True

    eth_feed = _Feed()
    eng._eth_price = eth_feed
    eng._eth_hourly_price = eth_feed
    eng.overlay = None
    eng._hydrate_window_books = lambda w: w  # noqa: E731
    fair = eng._osmani_fair_p(eth_w, 1_000_900.0)
    assert fair is not None
    assert eth_feed.snapped == ["eth-evt"]
    # Fair should reflect ETH above open (slightly > 0.5), not book mid alone.
    assert fair > 0.5

