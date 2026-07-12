"""Tests for Phase 1 directional cell learning table (observe-only)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from engine.pulse.directional_cell_learning import (
    CellKey,
    DirectionalCellLearningStore,
    PHASE1_SERIES_SLUGS,
    apply_phase2_to_tier_decision,
    ask_band_from_price,
    asset_from_series,
    minute_band_from_seconds,
    tv_pattern_from_ladder,
)
from engine.pulse.signal_edge import FADE, FOLLOW
from engine.pulse.tier_engine import Regime, Tier, TierDecision


def test_minute_bands_phase1():
    assert minute_band_from_seconds(60) == "0-5m"
    assert minute_band_from_seconds(8 * 60) == "5-12m"
    assert minute_band_from_seconds(20 * 60) == "12-30m"
    assert minute_band_from_seconds(35 * 60) == "30-45m"
    assert minute_band_from_seconds(50 * 60) == "45-55m"
    assert minute_band_from_seconds(56 * 60) == "other"


def test_ask_bands():
    assert ask_band_from_price(0.50) == "sweet"
    assert ask_band_from_price(0.40) == "mid"
    assert ask_band_from_price(0.80) == "tail"
    assert ask_band_from_price(None) == "unknown"


def test_tv_pattern_encoding():
    ladder_align = {"5": {"direction": "UP", "strength": 0.9},
                    "15": {"direction": "UP", "strength": 0.85},
                    "30": {"direction": "UP", "strength": 0.82}}
    assert tv_pattern_from_ladder(ladder_align, "up") == "S+"
    assert tv_pattern_from_ladder(ladder_align, "down") == "S-"

    ladder_weak = {"15": {"direction": "DOWN", "strength": 0.4}}
    assert tv_pattern_from_ladder(ladder_weak, "down") == "W+"

    ladder_conflict = {"5": {"direction": "UP", "strength": 0.9},
                       "15": {"direction": "DOWN", "strength": 0.9}}
    assert tv_pattern_from_ladder(ladder_conflict, "up") == "C"
    assert tv_pattern_from_ladder({}, "up") == "empty"
    assert tv_pattern_from_ladder(ladder_align, "up", information_I=0.50) == "H"


def test_asset_from_series():
    assert asset_from_series("btc-up-or-down-hourly") == "btc"
    assert asset_from_series("eth-up-or-down-4h") == "eth"
    assert asset_from_series("", "eth_1h") == "eth"


def test_cell_key_roundtrip():
    k = CellKey("btc", "5-12m", "trend_up", "S+", "sweet")
    assert CellKey.from_str(k.as_str()) == k


def test_store_eval_and_settle():
    with tempfile.TemporaryDirectory() as td:
        store = DirectionalCellLearningStore(Path(td), min_samples=5)
        key = CellKey("btc", "0-5m", "trend_up", "W+", "sweet")
        store.log_eval("win1", key, tier="probe", side="up", edge=0.03, p_up=0.55,
                       series_slug="btc-up-or-down-hourly", traded=True)
        store.record_settled("win1", won=True, pnl_usd=2.5)
        store.log_eval("win2", key, tier="probe", side="up", edge=0.02, p_up=0.52,
                       series_slug="btc-up-or-down-hourly", traded=True)
        store.record_settled("win2", won=False, pnl_usd=-3.0)

        stats = store.get(key)
        assert stats.evals == 2
        assert stats.trades == 2
        assert stats.wins == 1
        assert round(stats.pnl_usd, 2) == -0.5

        rep = store.report()
        assert rep["observe_only"] is True
        assert rep["affects_trading"] is False
        assert rep["study_series"] == list(PHASE1_SERIES_SLUGS)

        store2 = DirectionalCellLearningStore(Path(td))
        assert store2.get(key).trades == stats.trades


def test_tv_pattern_includes_45m():
    ladder = {"5": {"direction": "UP", "strength": 0.9},
              "15": {"direction": "UP", "strength": 0.85},
              "30": {"direction": "UP", "strength": 0.82},
              "45": {"direction": "DOWN", "strength": 0.9}}
    assert tv_pattern_from_ladder(ladder, "up") == "C"


def test_phase2_posterior_follow_boosts_size():
    td = TierDecision(tier=Tier.HARVEST, side="up", p_up=0.58, edge=0.05, conviction=0.16,
                      size_usd=20.0, regime=Regime.TREND_UP, reason="regime_bias_sweet")
    adj = {"enabled": True, "verdict": FOLLOW, "logit_shift": 0.20, "size_mult": 1.20,
           "cell": "btc|5-12m|trend_up|S+|sweet", "trades": 35}
    out = apply_phase2_to_tier_decision(td, adj, ask_up=0.53, ask_down=0.48, down_only=False)
    assert out.p_up > 0.58
    assert out.size_usd > 20.0
    assert out.breakdown.get("cell_phase2", {}).get("verdict") == FOLLOW


def test_phase2_posterior_fade_cuts_size():
    td = TierDecision(tier=Tier.PROBE, side="up", p_up=0.55, edge=0.03, conviction=0.10,
                      size_usd=5.0, regime=Regime.CHOP, reason="early_probe")
    adj = {"enabled": True, "verdict": FADE, "logit_shift": -0.30, "size_mult": 0.45,
           "cell": "btc|12-30m|chop|W+|mid", "trades": 40}
    out = apply_phase2_to_tier_decision(td, adj, ask_up=0.52, ask_down=0.50, down_only=False)
    assert out.p_up < 0.55
    assert out.size_usd < 5.0
    assert out.breakdown.get("cell_phase2", {}).get("verdict") == FADE


def test_phase2_adjustment_from_store():
    with tempfile.TemporaryDirectory() as td:
        store = DirectionalCellLearningStore(Path(td), min_samples=5)
        key = CellKey("btc", "0-5m", "trend_up", "W+", "sweet")
        for i in range(6):
            store.log_eval("w%d" % i, key, tier="probe", side="up", edge=0.02, p_up=0.52,
                           series_slug="btc-up-or-down-hourly", traded=True)
            store.record_settled("w%d" % i, won=True, pnl_usd=1.0)
        adj = store.phase2_adjustment(key)
        assert adj["enabled"] is True
        assert adj["verdict"] == FOLLOW
        assert adj["logit_shift"] > 0


def test_store_state_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        store = DirectionalCellLearningStore(Path(td))
        key = CellKey("eth", "12-30m", "chop", "C", "mid")
        store.log_eval("e1", key, tier="wait", side=None, edge=-0.01, p_up=0.5,
                       series_slug="eth-up-or-down-hourly", traded=False)
        st = store.to_state()
        store2 = DirectionalCellLearningStore(None)
        store2.load_state(st)
        assert store2.get(key).evals == 1
        assert store2.get(key).trades == 0
