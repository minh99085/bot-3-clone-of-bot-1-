"""Discovery + loop smoke tests."""

from __future__ import annotations

from hermes.discovery import (
    detect_regime,
    filter_candidates,
    load_edge_buckets_from_alpha,
    score_candidate,
    _synthetic_candidates,
)
from hermes.models import Regime
from hermes.signal_generator import dynamic_down_bias, generate_signal


def test_regime_detection():
    assert detect_regime(0.5, 100, 500) in (Regime.HIGH_VOL, Regime.LOW_VOL)
    r = detect_regime(0.5, 60_000, 50, price_change_1h=0.1)
    assert r == Regime.TRENDING_UP


def test_synthetic_discovery_filters():
    buckets = load_edge_buckets_from_alpha()
    raw = _synthetic_candidates()
    assert len(raw) >= 3
    filtered = filter_candidates(raw, buckets, min_score=0.2)
    assert len(filtered) >= 1
    assert score_candidate(filtered[0], buckets) >= 0.2


def test_down_bias_dynamic():
    assert dynamic_down_bias(Regime.TRENDING_DOWN, {"down_bias": 0.35}) > 0.35
    assert dynamic_down_bias(Regime.TRENDING_UP, {"down_bias": 0.35}) < 0.35


def test_signal_gen_skips_gated_osmani(monkeypatch):
    from hermes import signal_generator as sg
    from hermes.models import EntryMode, LaneStatus, MarketCandidate

    monkeypatch.setitem(sg.LANE_STATUS, EntryMode.MEAN_REVERSION, LaneStatus.ACTIVE)
    c = _synthetic_candidates()[0]
    c.regime = Regime.MEAN_REVERT
    buckets = load_edge_buckets_from_alpha()
    sig = generate_signal(
        c,
        alpha_text="DOWN bias",
        buckets=buckets,
        state={"capital_usd": 10_000, "down_bias": 0.35},
        paper=True,
    )
    # May be None if edge<=0; if present, must not be osmani
    if sig is not None:
        assert sig.entry_mode != EntryMode.OSMANI_LANE
