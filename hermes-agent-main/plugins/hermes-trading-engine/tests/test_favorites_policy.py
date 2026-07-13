"""Tests for favorites A/B policy (Profile B) on Osmani path."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from engine.pulse.directional_cell_learning import CellKey, DirectionalCellLearningStore
from engine.pulse.favorites_policy import (
    ab_profile_from_env,
    evaluate_osmani_fill,
    favorites_policy_active,
    ledger_ab_stats,
    min_entry_price_from_env,
)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.signal_edge import FADE, FOLLOW


def _window(*, slug: str = "btc-up-or-down-15m", ws: int = 900) -> PulseWindow:
    book = OrderBook(
        best_bid=0.48,
        best_ask=0.50,
        ask_depth_usd=5000.0,
        bid_depth_usd=5000.0,
        asks=[(0.50, 10000.0)],
        bids=[(0.48, 10000.0)],
    )
    return PulseWindow(
        event_id="evt-1",
        market_id="m1",
        slug="btc-updown-15m-1000000",
        title="BTC 15m",
        open_ts=1_000_000.0,
        close_ts=1_000_000.0 + ws,
        up_token_id="up",
        down_token_id="dn",
        series_slug=slug,
        window_seconds=ws,
        up_book=book,
        down_book=book,
    )


def test_policy_off_by_default(monkeypatch):
    monkeypatch.delenv("PULSE_AB_PROFILE", raising=False)
    monkeypatch.delenv("PULSE_FAVORITES_POLICY", raising=False)
    assert not favorites_policy_active()
    res = evaluate_osmani_fill(side="up", ask=0.40, window=_window(), now=1_000_100.0)
    assert res.allow is True
    assert res.reason == "policy_off"


def test_favorites_blocks_below_min_entry(monkeypatch):
    monkeypatch.setenv("PULSE_AB_PROFILE", "favorites")
    monkeypatch.setenv("PULSE_MIN_ENTRY_PRICE", "0.58")
    assert favorites_policy_active()
    assert min_entry_price_from_env() == 0.58
    res = evaluate_osmani_fill(side="up", ask=0.42, window=_window(), now=1_000_100.0)
    assert res.allow is False
    assert res.reason == "favorites_min_entry"
    assert res.ab_profile == "favorites"


def test_favorites_allows_at_floor(monkeypatch):
    monkeypatch.setenv("PULSE_AB_PROFILE", "favorites")
    monkeypatch.setenv("PULSE_MIN_ENTRY_PRICE", "0.58")
    res = evaluate_osmani_fill(side="up", ask=0.58, window=_window(), now=1_000_100.0)
    assert res.allow is True
    assert res.reason == "favorites_ok"


def test_cell_phase2_fade_blocks(monkeypatch):
    monkeypatch.setenv("PULSE_AB_PROFILE", "favorites")
    monkeypatch.setenv("PULSE_MIN_ENTRY_PRICE", "0.50")
    monkeypatch.setenv("PULSE_CELL_PHASE2_BLOCK_FADE", "1")
    with tempfile.TemporaryDirectory() as td:
        store = DirectionalCellLearningStore(Path(td), min_samples=3)
        key = CellKey("btc", "0-5m", "unknown", "∅", "sweet", horizon="15m", side="up")
        for i in range(4):
            store.log_eval(f"w{i}", key, tier="probe", side="up", edge=0.02, p_up=0.52,
                           series_slug="btc-up-or-down-15m", traded=True)
            store.record_settled(f"w{i}", won=False, pnl_usd=-2.0)
        res = evaluate_osmani_fill(
            side="up",
            ask=0.52,
            window=_window(),
            now=1_000_100.0,
            cell_learning=store,
            cell_phase2_enabled=True,
        )
        assert res.allow is False
        assert res.reason == "cell_phase2_fade"
        assert res.cell_verdict == FADE


def test_cell_phase2_follow_boosts_size(monkeypatch):
    monkeypatch.setenv("PULSE_AB_PROFILE", "favorites")
    monkeypatch.setenv("PULSE_MIN_ENTRY_PRICE", "0.50")
    with tempfile.TemporaryDirectory() as td:
        store = DirectionalCellLearningStore(Path(td), min_samples=3)
        key = CellKey("btc", "0-5m", "unknown", "∅", "sweet", horizon="15m", side="up")
        for i in range(5):
            store.log_eval(f"w{i}", key, tier="harvest", side="up", edge=0.04, p_up=0.58,
                           series_slug="btc-up-or-down-15m", traded=True)
            store.record_settled(f"w{i}", won=True, pnl_usd=3.0)
        res = evaluate_osmani_fill(
            side="up",
            ask=0.52,
            window=_window(),
            now=1_000_100.0,
            cell_learning=store,
            cell_phase2_enabled=True,
        )
        assert res.allow is True
        assert res.cell_verdict == FOLLOW
        assert res.size_mult >= 1.0


def test_ledger_ab_stats_by_profile():
    positions = {
        "a": SimpleNamespace(
            status="settled", won=True, pnl_usd=2.5,
            research={"ab_profile": "favorites"}),
        "b": SimpleNamespace(
            status="settled", won=False, pnl_usd=-1.0,
            research={"ab_profile": "favorites"}),
        "c": {"status": "settled", "won": True, "pnl_usd": 1.0,
              "research": {"ab_profile": "throughput"}},
        "d": {"status": "open", "won": None, "pnl_usd": 0,
              "research": {"ab_profile": "favorites"}},
    }
    stats = ledger_ab_stats(positions)
    assert stats["favorites"]["n"] == 2
    assert stats["favorites"]["wins"] == 1
    assert stats["favorites"]["wr"] == 0.5
    assert stats["favorites"]["open"] == 1
    assert stats["throughput"]["n"] == 1
    assert stats["throughput"]["wr"] == 1.0


def test_ab_profile_aliases(monkeypatch):
    monkeypatch.setenv("PULSE_AB_PROFILE", "profile_b")
    assert favorites_policy_active()
    assert ab_profile_from_env() == "profile_b"
