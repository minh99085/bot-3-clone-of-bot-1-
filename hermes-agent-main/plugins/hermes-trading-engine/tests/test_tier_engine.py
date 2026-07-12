"""Tests for the Directional Tier Engine (PAPER ONLY)."""

from engine.pulse.tier_engine import (
    DirectionalTierEngine,
    Regime,
    Tier,
    TierConfig,
    classify_regime,
)

NOW = 1_000_000.0


def _tv(dirs, strength=0.7, age=60.0):
    """dirs: {tf: 'UP'|'DOWN'|'FLAT'} -> snapshot dict."""
    return {tf: {"direction": d, "strength": strength, "ts": NOW - age} for tf, d in dirs.items()}


def _all_up():
    return _tv({"5": "UP", "15": "UP", "30": "UP", "60": "UP", "240": "UP", "1440": "UP"})


def test_regime_trend_up_down_chop_neutral():
    assert classify_regime({"240": (1, 0.7), "60": (1, 0.6), "1440": (1, 0.5)}) == Regime.TREND_UP
    assert classify_regime({"240": (-1, 0.7), "60": (-1, 0.6), "1440": (-1, 0.5)}) == Regime.TREND_DOWN
    # 4h up but 1h down -> conflict -> chop
    assert classify_regime({"240": (1, 0.7), "60": (-1, 0.6), "1440": (0, 0.0)}) == Regime.CHOP
    # weak 4h -> chop
    assert classify_regime({"240": (1, 0.2), "60": (1, 0.3), "1440": (0, 0.0)}) == Regime.CHOP
    # no 4h, no 1h conviction -> neutral
    assert classify_regime({"240": (0, 0.0), "60": (0, 0.0), "1440": (0, 0.0)}) == Regime.NEUTRAL


def test_snipe_fires_late_window_decisive():
    eng = DirectionalTierEngine(TierConfig())
    d = eng.evaluate(window_key="w", sso=3400, ttc_s=200, s_now=64300.0, s_open=64000.0,
                     sigma_per_sec=6.7e-5, ask_up=0.90, ask_down=0.10, tv_by_tf=_all_up(),
                     now=NOW, ask_depth_up=2000, ask_depth_down=2000)
    assert d.tier == Tier.SNIPE and d.side == "up"
    assert abs(d.z) >= 2.3 and d.edge > 0 and d.size_usd > 0


def test_snipe_jump_veto():
    eng = DirectionalTierEngine(TierConfig())
    d = eng.evaluate(window_key="w", sso=3400, ttc_s=200, s_now=64300.0, s_open=64000.0,
                     sigma_per_sec=6.7e-5, ask_up=0.90, ask_down=0.10, tv_by_tf=_all_up(),
                     now=NOW, jump_risk=True)
    assert d.tier == Tier.WAIT and d.reason == "snipe_jump_veto"


def test_strike_fires_mid_window_aligned_trend():
    eng = DirectionalTierEngine(TierConfig())
    d = eng.evaluate(window_key="w", sso=1500, ttc_s=2100, s_now=64100.0, s_open=64000.0,
                     sigma_per_sec=6.7e-5, ask_up=0.55, ask_down=0.49, tv_by_tf=_all_up(),
                     now=NOW, ask_depth_up=2000, ask_depth_down=2000)
    assert d.tier == Tier.STRIKE and d.side == "up" and d.regime == Regime.TREND_UP
    assert d.size_usd > 25.0                       # bigger than harvest cap


def test_chop_regime_does_not_strike():
    eng = DirectionalTierEngine(TierConfig())
    # 4h up but 1h down -> chop; momentum should be faded, no strike
    tv = _all_up()
    tv["60"] = {"direction": "DOWN", "strength": 0.6, "ts": NOW - 300}
    d = eng.evaluate(window_key="w", sso=1500, ttc_s=2100, s_now=64100.0, s_open=64000.0,
                     sigma_per_sec=6.7e-5, ask_up=0.55, ask_down=0.49, tv_by_tf=tv,
                     now=NOW, ask_depth_up=2000, ask_depth_down=2000)
    assert d.regime == Regime.CHOP
    assert d.tier != Tier.STRIKE


def test_watching_floor_blocks_early():
    eng = DirectionalTierEngine(TierConfig())
    d = eng.evaluate(window_key="w", sso=60, ttc_s=3540, s_now=64050.0, s_open=64000.0,
                     sigma_per_sec=6.7e-5, ask_up=0.55, ask_down=0.49, tv_by_tf=_all_up(), now=NOW)
    assert d.tier == Tier.WAIT and d.reason == "watching_floor"


def test_negative_edge_waits():
    eng = DirectionalTierEngine(TierConfig())
    # expensive both sides -> negative edge
    d = eng.evaluate(window_key="w", sso=1500, ttc_s=2100, s_now=64000.0, s_open=64000.0,
                     sigma_per_sec=6.7e-5, ask_up=0.97, ask_down=0.97, tv_by_tf=_all_up(), now=NOW)
    assert d.tier == Tier.WAIT


def test_down_only_forces_down_side():
    eng = DirectionalTierEngine(TierConfig())
    d = eng.evaluate(window_key="w", sso=3400, ttc_s=200, s_now=63700.0, s_open=64000.0,
                     sigma_per_sec=6.7e-5, ask_up=0.10, ask_down=0.90,
                     tv_by_tf=_tv({"5": "DOWN", "15": "DOWN", "30": "DOWN",
                                   "60": "DOWN", "240": "DOWN", "1440": "DOWN"}),
                     now=NOW, down_only=True, ask_depth_down=2000)
    assert d.side == "down" and d.tier == Tier.SNIPE


def test_sizing_kelly_and_caps():
    eng = DirectionalTierEngine(TierConfig(bankroll_usd=2000.0))
    # huge edge + huge depth -> should hit the tier cap, not blow up
    d = eng.evaluate(window_key="w", sso=3400, ttc_s=120, s_now=64400.0, s_open=64000.0,
                     sigma_per_sec=6.7e-5, ask_up=0.80, ask_down=0.20, tv_by_tf=_all_up(),
                     now=NOW, ask_depth_up=1_000_000)
    assert d.tier == Tier.SNIPE and d.size_usd <= 200.0 + 1e-6      # snipe cap

    # depth cap binds when book is thin
    d2 = eng.evaluate(window_key="w2", sso=3400, ttc_s=120, s_now=64400.0, s_open=64000.0,
                      sigma_per_sec=6.7e-5, ask_up=0.80, ask_down=0.20, tv_by_tf=_all_up(),
                      now=NOW, ask_depth_up=40.0)
    assert d2.size_usd <= 0.25 * 40.0 + 1e-6


def test_daily_loss_halt():
    eng = DirectionalTierEngine(TierConfig(bankroll_usd=2000.0, daily_loss_halt_pct=0.10))
    eng.record_pnl(-201.0, now=NOW)                # > 10% of $2000 = $200
    d = eng.evaluate(window_key="w", sso=3400, ttc_s=120, s_now=64400.0, s_open=64000.0,
                     sigma_per_sec=6.7e-5, ask_up=0.80, ask_down=0.20, tv_by_tf=_all_up(), now=NOW)
    assert d.tier == Tier.WAIT and d.reason == "daily_loss_halt"


def test_lr_grading_and_recalibrate(tmp_path):
    eng = DirectionalTierEngine(TierConfig(), data_dir=tmp_path)
    # make a decision, then settle it as a win -> grades the aligned TFs
    entry = eng.evaluate(window_key="w", sso=1500, ttc_s=2100, s_now=64100.0, s_open=64000.0,
                         sigma_per_sec=6.7e-5, ask_up=0.55, ask_down=0.49,
                         tv_by_tf=_all_up(), now=NOW)
    eng.record_entry("w", entry)
    eng.record_settled("w", won=True, pnl_usd=45.0, now=NOW)
    assert (tmp_path / "tier_lr_table.json").exists()
    assert any(g["aligned_n"] > 0 for g in eng.lrs.grades.values())
    eng.recalibrate()                              # should not raise; small-n cells unchanged

    eng2 = DirectionalTierEngine(TierConfig(), data_dir=tmp_path)   # persistence roundtrip
    assert eng2.lrs.grades


def test_settlement_grades_frozen_entry_not_later_tick():
    eng = DirectionalTierEngine(TierConfig())
    entry = eng.evaluate(window_key="w", sso=1500, ttc_s=2100, s_now=64100.0,
                         s_open=64000.0, sigma_per_sec=6.7e-5,
                         ask_up=0.55, ask_down=0.49, tv_by_tf=_all_up(), now=NOW)
    eng.record_entry("w", entry)
    frozen_side = eng._last_decision["w"].side
    eng.evaluate(window_key="w", sso=2500, ttc_s=1100, s_now=63900.0,
                 s_open=64000.0, sigma_per_sec=6.7e-5,
                 ask_up=0.49, ask_down=0.55,
                 tv_by_tf=_tv({"5": "DOWN", "15": "DOWN", "30": "DOWN"}), now=NOW)
    assert eng._last_decision["w"].side == frozen_side


def test_report_shape():
    eng = DirectionalTierEngine(TierConfig())
    eng.evaluate(window_key="w", sso=3400, ttc_s=120, s_now=64400.0, s_open=64000.0,
                 sigma_per_sec=6.7e-5, ask_up=0.80, ask_down=0.20, tv_by_tf=_all_up(), now=NOW)
    r = eng.to_report()
    assert r["enabled"] is True and r["bankroll_usd"] == 2000.0
    assert "tier_counts" in r and "lr_table" in r


def test_tier_config_from_env_high_wr_sweet(monkeypatch):
    """Throughput mode wires wide sweet band + early watching floor via env."""
    monkeypatch.setenv("PULSE_TIER_SWEET_MIN", "0.45")
    monkeypatch.setenv("PULSE_TIER_SWEET_MAX", "0.85")
    monkeypatch.setenv("PULSE_TIER_MIN_SECONDS_SINCE_OPEN", "300")
    monkeypatch.setenv("PULSE_TIER_STRIKE_EDGE_MIN", "0.03")
    monkeypatch.setenv("PULSE_TIER_HARVEST_EDGE_MIN", "0.02")
    monkeypatch.setenv("PULSE_TIER_SNIPE_MAX_USD", "25")
    monkeypatch.setenv("PULSE_TIER_STRIKE_MAX_USD", "15")
    monkeypatch.setenv("PULSE_TIER_HARVEST_MAX_USD", "10")
    cfg = TierConfig.from_env()
    assert cfg.sweet_min == 0.45
    assert cfg.sweet_max == 0.85
    assert cfg.min_seconds_since_open == 300.0
    assert cfg.strike_edge_min == 0.03
    assert cfg.harvest_edge_min == 0.02
    assert cfg.snipe_max_usd == 25.0
    assert cfg.strike_max_usd == 15.0
    assert cfg.harvest_max_usd == 10.0


# --------------------------------------------------------------------------------------------- #
# Engine integration: the tier engine DRIVES a paper directional fill through execution_gate.
# --------------------------------------------------------------------------------------------- #

class _StubTVLadder:
    def __init__(self, t0):
        self.t0 = t0
    def drain_pending(self):
        return []
    def latest_feature(self, **kw):
        return {"direction": "UP", "strength": 0.7, "age_s": 1.0}
    def latest_feature_for_symbol(self, sym, **kw):
        return self.latest_feature()
    def alert_history_for_symbol(self, symbol, **kw):
        return []
    def report(self):
        lbt = {("BTCUSD@%s" % tf): {"direction": "UP", "strength": 0.7, "ts": self.t0 + 1500 - 60}
               for tf in ("5", "15", "30", "60", "240", "1440")}
        return {"enabled": True, "tradingview_observe_only": True,
                "tradingview_alerts_received": 6, "tradingview_alerts_valid": 6,
                "tradingview_alerts_rejected": 0, "tradingview_reject_reasons": {},
                "tradingview_latest_signal": None, "tradingview_latest_by_timeframe": lbt,
                "tradingview_mtf_timeframes": ["5", "15", "30", "60", "240", "1440"]}


def test_engine_tier_evaluates_without_bypassing_directional_gates(tmp_path):
    from engine.pulse.engine import PulseConfig, PulseEngine
    from engine.pulse.fair_value import RollingVol
    from engine.pulse.markets import OrderBook, PulseWindow
    from engine.pulse.price import PulsePriceFeed

    t0 = 8_000_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="btc-up-or-down-hourly", title="BTC",
                      open_ts=t0, close_ts=t0 + 3600, up_token_id="U", down_token_id="D",
                      window_seconds=3600, series_slug="btc-up-or-down-hourly", series_label="btc_1h",
                      directional_lane=True)
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 1.5              # steady rise -> up displacement, aligned with UP TV
        return price["p"]

    class _Mkt:
        def active_windows(self, now=None, **kw):
            return [win]
        def owns(self, w):
            return True
        def hydrate_books(self, w):
            # cheap UP ask vs a rising price -> positive up edge for the tier engine
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=3000,
                                  bid_depth_usd=3000, bids=[(0.50, 5000)], asks=[(0.55, 5000)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=3000,
                                    bid_depth_usd=3000, bids=[(0.44, 5000)], asks=[(0.49, 5000)])
            return w
        def resolve_up(self, *a, **k):
            return True
        def report(self):
            return {"enabled": True, "stub": True}

    feed = PulsePriceFeed(fetcher=fetch, vol=RollingVol(window_s=1800, min_samples=8),
                          max_open_lag_s=600.0)
    eng = PulseEngine(
        PulseConfig(tick_seconds=1.0, size_usd=5.0, min_seconds_since_open=0.0,
                    sigma_trust_floor=0.0, min_vol_samples=2, directional_down_only=False,
                    directional_block_up_until_promoted=False,
                    directional_up_restrictions_enabled=False, tv_mtf_conflict_gate_enabled=False,
                    tv_down_bias_gate_enabled=False, tier_engine_enabled=True,
                    rtds_enabled=False, directional_require_winning_bucket=False,
                    # Osmani loop defaults ON and skips tick fills unless legacy_tick is set.
                    directional_legacy_tick=True, osmani_loop_enabled=False,
                    starting_capital_usd=2000.0, directional_max_bankroll_frac=0.35,
                    correlated_exposure_cap_usd=300.0, data_dir=str(tmp_path), fresh_start=True),
        market_feed=_Mkt(), price_feed=feed)
    # Directional windows now come from _directional_hourly_feed (not legacy market_feed).
    eng._directional_hourly_feed = _Mkt()
    eng.tradingview = _StubTVLadder(t0)

    for i in range(20):                       # warm vol before open
        eng.tick(now=t0 - 20 + i)
    eng.tick(now=t0 + 2)                       # capture open snapshot (lag 2s)
    for k in range(1, 300):                    # dense ticks into the mid window (~25 min)
        eng.tick(now=t0 + 2 + k * 5)

    rep = eng.status()["tier_engine"]
    assert rep["enabled"] is True
    assert rep["total_decisions"] >= 1         # the tier engine evaluated candidates
    # Tier classification must not manufacture CEX-lead authority or bypass the
    # shared calibration/selectivity/execution gates.  This synthetic lane is
    # intentionally unproven, so strict abstention is the correct result.
    assert eng.ledger.trades == 0
