"""Tests for 15m directional feed + lane strategy learner (PAPER ONLY)."""

from __future__ import annotations

import json

from engine.pulse.directional_15m_feed import Directional15mMarketFeed, WINDOW_SECONDS
from engine.pulse.lane_15m_learner import (
    Lane15mLearnerConfig,
    Lane15mPolicy,
    Lane15mStrategyLearner,
)
from engine.pulse.tier_engine import DirectionalTierEngine, Tier, TierConfig


def _http_factory(fixtures: dict):
    def _http(url: str, params: dict):
        series = params.get("series_slug")
        if series and series in fixtures:
            return 200, fixtures[series]
        return 404, None
    return _http


def test_15m_feed_discovers_btc_and_eth():
    now = 1_783_649_700.0  # aligned with polymarket slug epoch style
    close = now + 600
    open_ts = close - 900
    from datetime import datetime, timezone

    def iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def mk_ev(asset, eid, slug):
        return {
            "id": eid,
            "slug": slug,
            "title": "%s Up or Down 15m" % asset.upper(),
            "endDate": iso(close),
            "markets": [{
                "id": "m" + eid,
                "slug": slug,
                "question": "%s 15m" % asset,
                "endDate": iso(close),
                "startDate": iso(open_ts - 86400),  # Gamma often has early startDate
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["u%s", "d%s"]' % (eid, eid),
            }],
        }

    fixtures = {
        "btc-up-or-down-15m": [mk_ev("btc", "1", "btc-updown-15m-x")],
        "eth-up-or-down-15m": [mk_ev("eth", "2", "eth-updown-15m-x")],
    }
    feed = Directional15mMarketFeed(
        auto_discover=True, assets=("btc", "eth"), http_get=_http_factory(fixtures))
    wins = feed.active_windows(now=now)
    assert len(wins) == 2
    assert {w.series_slug for w in wins} == {"btc-up-or-down-15m", "eth-up-or-down-15m"}
    for w in wins:
        assert w.directional_lane is True
        assert w.window_seconds == WINDOW_SECONDS
        assert abs(w.close_ts - w.open_ts - 900) < 1.0
        assert feed.owns(w)


def test_tier_scales_for_15m_window():
    """STRIKE SSO floor scales; 15m mid-window can strike without needing sso>=720."""
    eng = DirectionalTierEngine(TierConfig(min_seconds_since_open=300))
    now = 1_000_000.0
    tv = {tf: {"direction": "UP", "strength": 0.7, "ts": now - 60}
          for tf in ("5", "15", "30", "60", "240", "1440")}
    # 15m mid-window: sso=400 (would fail unscaled strike_sso_min=720)
    d = eng.evaluate(
        window_key="w15", sso=400, ttc_s=500, s_now=64100.0, s_open=64000.0,
        sigma_per_sec=6.7e-5, ask_up=0.55, ask_down=0.49, tv_by_tf=tv, now=now,
        ask_depth_up=2000, ask_depth_down=2000, window_seconds=900.0)
    assert d.tier in (Tier.STRIKE, Tier.HARVEST, Tier.PROBE, Tier.SNIPE)
    assert d.breakdown.get("scale") == 0.25


def test_tier_15m_overlay_raises_watch_floor():
    eng = DirectionalTierEngine(TierConfig())
    now = 1_000_000.0
    tv = {tf: {"direction": "UP", "strength": 0.7, "ts": now - 60}
          for tf in ("5", "15", "30", "60", "240", "1440")}
    d = eng.evaluate(
        window_key="w", sso=40, ttc_s=860, s_now=64050.0, s_open=64000.0,
        sigma_per_sec=6.7e-5, ask_up=0.55, ask_down=0.49, tv_by_tf=tv, now=now,
        window_seconds=900.0, overlay={"min_sso": 60.0})
    assert d.tier == Tier.WAIT and d.reason == "watching_floor"


def test_lane_learner_tightens_on_low_wr():
    learner = Lane15mStrategyLearner(
        Lane15mLearnerConfig(min_samples=8, kill_wr=0.50, cooldown_settlements=2,
                             target_wr=0.65, side_min_n=4),
        Lane15mPolicy(side_mode="both", min_edge=0.02, sweet_min=0.45, sweet_max=0.85),
    )
    # Mostly losing UP trades, winning DOWN
    for i in range(6):
        learner.record_settled(won=False, pnl_usd=-5.0, side="up", entry_price=0.52,
                               asset="btc", sso=200, ttc_s=700, now=1000.0 + i)
    for i in range(6):
        learner.record_settled(won=True, pnl_usd=4.5, side="down", entry_price=0.52,
                               asset="btc", sso=500, ttc_s=200, now=2000.0 + i)
    adj = learner.maybe_adjust()
    assert adj is not None
    assert adj["action"] in ("tighten", "rebalance")
    assert learner.policy.side_mode in ("down_only", "down_bias")
    ok, _ = learner.filter_side("up")
    if learner.policy.side_mode == "down_only":
        assert ok is False
    else:
        assert learner.side_size_mult("up") < 1.0


def test_lane_learner_persistence_roundtrip():
    learner = Lane15mStrategyLearner()
    learner.record_settled(won=True, pnl_usd=5.0, side="down", entry_price=0.5,
                           asset="eth", sso=300, ttc_s=600, now=1.0)
    state = learner.to_state()
    learner2 = Lane15mStrategyLearner()
    learner2.load_state(state)
    assert learner2.policy.side_mode == learner.policy.side_mode
    assert len(learner2._recent) == 1
    rep = learner2.report()
    assert rep["enabled"] is True
    assert "policy" in rep
