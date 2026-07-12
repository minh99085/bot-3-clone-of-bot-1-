"""LLM council: evidence-weighted ensemble of quant + Grok + Claude directional views (PAPER)."""

from __future__ import annotations

from engine.pulse.llm_council import (council_consensus, member_weight, LLMCouncil, best_ev_side,
                                       member_stance)


def test_member_stance_follow_fade_ignore_cold():
    # cold: not enough samples -> trust prior, no invert
    assert member_stance(5, 5, prior=0.3, min_samples=20)[0] == "cold"
    # follow: proven predictive (Wilson lower > 0.5)
    st, w, inv = member_stance(26, 30, prior=0.4, min_samples=20)
    assert st == "follow" and inv is False and w > 0.1
    # fade: proven anti-predictive (Wilson upper < 0.5) -> invert
    st, w, inv = member_stance(4, 30, prior=0.4, min_samples=20)
    assert st == "fade" and inv is True and w > 0.1
    # ignore: spans 0.5 -> floor, no invert
    st, w, inv = member_stance(15, 30, prior=0.4, min_samples=20)
    assert st == "ignore" and inv is False and w == 0.1


def test_council_grades_each_tv_timeframe_independently():
    c = LLMCouncil(enabled=True, min_samples=20, min_members=1, min_margin=0.0)
    # tv_2m contrarian (says UP, market DOWN); tv_15m predictive (says DOWN, market DOWN).
    for _ in range(30):
        c.grade({"tv_2m": 0.8, "tv_15m": 0.2}, outcome_up=False)
    rep = c.report()
    assert rep["members"]["tv_2m"]["stance"] == "fade"       # short-TF anti-predictive -> faded
    assert rep["members"]["tv_15m"]["stance"] == "follow"    # horizon-matched TF -> followed
    assert rep["members"]["tv_2m"]["prior"] == 0.1           # non-anchor members start at the floor


def test_council_forget_retires_members_and_blocks_repopulation():
    c = LLMCouncil(enabled=True, min_samples=20, min_members=1, min_margin=0.0)
    for _ in range(25):
        c.grade({"tv_2m": 0.8, "tv_15m": 0.2}, outcome_up=False)
    assert "tv_2m" in c.report()["members"]
    # retire tv_2m (operator removed the 2m timeframe)
    c.forget(["tv_2m"])
    rep = c.report()
    assert "tv_2m" not in rep["members"]          # purged from report
    assert "tv_15m" in rep["members"]             # survivor kept
    # an OLD pending snapshot that still references tv_2m must not repopulate it
    c.grade({"tv_2m": 0.8, "tv_15m": 0.2}, outcome_up=False)
    assert "tv_2m" not in c.report()["members"]
    # and tv_2m never contributes a vote to the consensus
    out = c.decide({"tv_2m": 0.99, "tv_15m": 0.2})
    assert "tv_2m" not in (out.get("stances") or {})


def test_reset_members_clears_stats_but_keeps_gradeable():
    c = LLMCouncil(enabled=True, min_samples=20, min_members=1, min_margin=0.0)
    for _ in range(30):
        c.grade({"tv_5m": 0.8}, outcome_up=True)
    assert c.report()["members"]["tv_5m"]["n"] == 30
    assert c.reset_members(["tv_5m"]) == 1
    assert "tv_5m" not in c.report()["members"]        # stats cleared
    c.grade({"tv_5m": 0.8}, outcome_up=True)            # still gradeable -> re-accumulates fresh
    assert c.report()["members"]["tv_5m"]["n"] == 1


def test_maybe_reset_is_one_time_per_token():
    c = LLMCouncil(enabled=True, min_samples=20, min_members=1, min_margin=0.0)
    for _ in range(10):
        c.grade({"tv_5m": 0.8, "tv_3m": 0.2}, outcome_up=True)
    assert c.maybe_reset("v1", ["tv_5m"]) is True          # first apply
    assert "tv_5m" not in c.report()["members"]
    assert "tv_3m" in c.report()["members"]                # untouched
    for _ in range(5):
        c.grade({"tv_5m": 0.8}, outcome_up=True)
    assert c.maybe_reset("v1", ["tv_5m"]) is False         # same token -> no re-reset
    assert c.report()["members"]["tv_5m"]["n"] == 5        # grades preserved
    assert c.maybe_reset("", ["tv_5m"]) is False           # empty token -> no-op
    assert c.maybe_reset("v2", ["tv_5m"]) is True          # new token -> resets again
    # reset_token survives a state roundtrip (won't re-run after restart)
    c2 = LLMCouncil(enabled=True)
    c2.load_state(c.to_state())
    assert c2.maybe_reset("v2", ["tv_5m"]) is False


def test_council_load_state_drops_ignored_members():
    c = LLMCouncil(enabled=True, min_samples=20, min_members=1, min_margin=0.0)
    c.forget(["tv_4m"])
    c.load_state({"stats": {"tv_4m": {"n": 30, "correct": 5}, "tv_15m": {"n": 30, "correct": 25}}})
    rep = c.report()
    assert "tv_4m" not in rep["members"] and "tv_15m" in rep["members"]


def test_council_fades_anti_predictive_member():
    c = LLMCouncil(enabled=True, min_samples=20, min_members=1, min_margin=0.0)
    # TV member is contrarian: says UP (p_up 0.8) but outcome is DOWN, repeatedly.
    for _ in range(30):
        c.grade({"tv": 0.8}, outcome_up=False)
    rep = c.report()
    assert rep["members"]["tv"]["stance"] == "fade" and rep["members"]["tv"]["faded"] is True
    # Now with only the faded TV member saying UP(0.8), the consensus should lean DOWN (inverted).
    out = c.decide({"tv": 0.8})
    assert out["consensus_p_up"] < 0.5
    assert out["stances"]["tv"]["effective_p_up"] < 0.5


def test_best_ev_picks_cheap_underdog_when_underpriced():
    # p_up=0.55, UP favorite priced 0.90 (ev -0.35), DOWN underdog priced 0.30 (ev 0.45-0.30=0.15).
    side, ev = best_ev_side(0.55, up_ask=0.90, down_ask=0.30, min_edge=0.01)
    assert side == "down" and ev == 0.15          # takes the cheap +EV underdog, not the favorite


def test_best_ev_picks_up_when_up_is_cheap_and_positive():
    side, ev = best_ev_side(0.60, up_ask=0.50, down_ask=0.55, min_edge=0.01)
    assert side == "up" and ev == 0.10


def test_best_ev_no_trade_when_both_overpriced():
    # both sides -EV (efficient/expensive market) -> no trade
    side, ev = best_ev_side(0.55, up_ask=0.62, down_ask=0.50, min_edge=0.01)
    assert side is None                            # best ev = -0.05 (down) < min_edge


def test_best_ev_handles_missing_book():
    side, ev = best_ev_side(0.7, up_ask=None, down_ask=0.20, min_edge=0.01)
    assert side == "down" and ev == 0.10
    assert best_ev_side(None, 0.5, 0.5)[0] is None


def test_consensus_trades_when_members_agree_with_margin():
    votes = [
        {"name": "quant", "p_up": 0.62, "weight": 1.0},
        {"name": "grok", "p_up": 0.60, "weight": 0.5},
        {"name": "claude", "p_up": 0.58, "weight": 0.7},
    ]
    out = council_consensus(votes, min_agreement=0.6, min_margin=0.02, min_members=2)
    assert out["trade"] is True
    assert out["side"] == "up"
    assert out["agreement"] == 1.0
    assert out["consensus_p_up"] > 0.5


def test_consensus_no_trade_insufficient_members():
    out = council_consensus([{"name": "quant", "p_up": 0.7, "weight": 1.0}], min_members=2)
    assert out["trade"] is False and out["reason"] == "insufficient_members"


def test_consensus_no_trade_low_margin():
    votes = [{"name": "quant", "p_up": 0.505, "weight": 1.0},
             {"name": "grok", "p_up": 0.51, "weight": 1.0}]
    out = council_consensus(votes, min_margin=0.05, min_members=2)
    assert out["trade"] is False and out["reason"] == "low_margin"


def test_consensus_no_trade_on_disagreement():
    # High-weight members split around 0.5 -> low weighted agreement.
    votes = [{"name": "quant", "p_up": 0.75, "weight": 1.0},
             {"name": "grok", "p_up": 0.25, "weight": 1.0}]
    out = council_consensus(votes, min_agreement=0.6, min_margin=0.0, min_members=2)
    # consensus is exactly 0.5 -> side up, but only half the weight agrees -> below 0.6
    assert out["agreement"] == 0.5
    assert out["trade"] is False and out["reason"] == "low_agreement"


def test_member_weight_cold_warm_and_antipredictive():
    # cold: below min_samples -> prior
    assert member_weight(0, 0, prior=0.4, floor=0.1, min_samples=20) == 0.4
    # warm anti-predictive (30% accuracy) -> collapses to floor
    assert member_weight(9, 30, prior=0.4, floor=0.1, min_samples=20, scale=8.0) == 0.1
    # warm proven (~80% accuracy) -> well above floor
    w = member_weight(24, 30, prior=0.4, floor=0.1, min_samples=20, scale=8.0)
    assert w > 0.5


def test_council_follows_proven_member_and_fades_anti_predictive_one():
    c = LLMCouncil(enabled=True, min_samples=20, min_members=2, min_margin=0.02)
    # grok is anti-predictive (always says up, market goes down); claude is accurate (says down).
    for _ in range(30):
        c.grade({"grok": 0.8, "claude": 0.2, "quant": 0.5}, outcome_up=False)
    rep = c.report()
    assert rep["members"]["grok"]["stance"] == "fade"              # proven anti-predictive -> faded
    assert rep["members"]["claude"]["stance"] == "follow"           # proven predictive -> followed
    # grok(up 0.70) is FADED to 0.30 (down), agreeing with claude(down 0.30): confident DOWN consensus.
    out = c.decide({"grok": 0.70, "claude": 0.30, "quant": 0.5})
    assert out["side"] == "down"
    assert out["trade"] is True
    assert out["stances"]["grok"]["effective_p_up"] < 0.5          # grok's UP view was inverted


def test_cold_member_cannot_swing_vote_and_quant_anchors():
    # A cold, ungraded member (e.g. an absent Claude or a fresh TV TF) with a confident view must NOT
    # move the consensus -- it sits at the floor and its view is shrunk toward neutral. Only the quant
    # anchor carries cold weight, so the council falls back to the quant baseline.
    c = LLMCouncil(enabled=True, min_members=2, min_margin=0.02, min_samples=20)
    out = c.decide({"quant": 0.44, "claude": 0.99})   # claude screams UP but is ungraded
    st = out["stances"]
    assert st["claude"]["weight"] == 0.1              # cold non-anchor -> floor weight
    assert st["claude"]["effective_p_up"] == 0.5      # cold view fully shrunk to neutral (n=0)
    assert st["quant"]["weight"] == 1.0               # anchor keeps its cold weight
    assert out["side"] == "down"                      # consensus follows the quant anchor (0.44), not claude


def test_fade_needs_more_evidence_than_follow():
    # anti-predictive at n=25 is NOT yet faded (fade_min_samples defaults to 30) -> ignore; at n=35 it fades.
    c = LLMCouncil(enabled=True, min_samples=20, min_members=1, min_margin=0.0)
    for _ in range(25):
        c.grade({"grok": 0.9}, outcome_up=False)
    assert c.report()["members"]["grok"]["stance"] == "ignore"   # anti-predictive but under fade bar
    for _ in range(10):
        c.grade({"grok": 0.9}, outcome_up=False)
    assert c.report()["members"]["grok"]["stance"] == "fade"     # now n=35 >= 30 -> fade


def test_council_fail_open_when_no_views():
    c = LLMCouncil(enabled=True, min_members=2)
    out = c.decide({"grok": None, "claude": None, "quant": None})
    assert out["trade"] is False and out["reason"] == "insufficient_members"


def test_state_roundtrip():
    c = LLMCouncil(enabled=True)
    c.grade({"quant": 0.6, "grok": 0.4}, outcome_up=True)
    st = c.to_state()
    c2 = LLMCouncil(enabled=True)
    c2.load_state(st)
    assert c2.to_state()["stats"] == st["stats"]
    assert c2.graded == 1


def test_from_env_reads_council_flags(monkeypatch):
    from engine.pulse.engine import PulseConfig
    monkeypatch.setenv("PULSE_LLM_COUNCIL_ENABLED", "1")
    monkeypatch.setenv("PULSE_LLM_COUNCIL_MIN_AGREEMENT", "0.7")
    monkeypatch.setenv("PULSE_CLAUDE_DECIDER_ENABLED", "1")
    c = PulseConfig.from_env()
    assert c.llm_council_enabled is True
    assert c.llm_council_min_agreement == 0.7
    assert c.claude_decider_enabled is True


def test_tv_mtf_view_combines_timeframes_by_agreement(tmp_path):
    from engine.pulse.engine import PulseEngine, PulseConfig
    from engine.pulse.price import PulsePriceFeed
    from engine.pulse.fair_value import RollingVol

    class _Mkt:
        def active_windows(self, now=None, **kw):
            return []

        def hydrate_books(self, w):
            return w

    feed = PulsePriceFeed(fetcher=lambda: 64000.0, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=2), max_open_lag_s=9999.0)
    cfg = PulseConfig(tick_seconds=1.0, data_dir=str(tmp_path), tradingview_webhook_port=0,
                      llm_council_enabled=True,
                      tradingview_mtf_timeframes=("5", "10", "15"))
    eng = PulseEngine(cfg, market_feed=_Mkt(), price_feed=feed)
    # unanimous UP across timeframes -> confident UP composite
    eng._tv_per_tf_views = lambda now, symbol=None: {"tv_5m": 0.85, "tv_10m": 0.85, "tv_15m": 0.85}
    assert eng._tv_mtf_view(0.0) > 0.8
    # fully split -> pulled toward neutral (weighted slower TFs may not land exactly 0.5)
    eng._tv_per_tf_views = lambda now, symbol=None: {"tv_5m": 0.85, "tv_10m": 0.15}
    assert abs(eng._tv_mtf_view(0.0) - 0.5) < 0.06
    # no fresh alerts -> None (no TV vote)
    eng._tv_per_tf_views = lambda now, symbol=None: {}
    assert eng._tv_mtf_view(0.0) is None


def _mini_engine(tmp_path, **cfg_over):
    from engine.pulse.engine import PulseEngine, PulseConfig
    from engine.pulse.price import PulsePriceFeed
    from engine.pulse.fair_value import RollingVol

    class _Mkt:
        def active_windows(self, now=None, **kw):
            return []

        def hydrate_books(self, w):
            return w

    feed = PulsePriceFeed(fetcher=lambda: 64000.0, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=2), max_open_lag_s=9999.0)
    cfg = PulseConfig(tick_seconds=1.0, data_dir=str(tmp_path), tradingview_webhook_port=0,
                      llm_council_enabled=True, **cfg_over)
    return PulseEngine(cfg, market_feed=_Mkt(), price_feed=feed)


def test_tv_freshness_cap_drops_stale_offgrid_read(tmp_path):
    import types
    eng = _mini_engine(tmp_path, council_tv_member=True, council_tv_max_age_s=900.0,
                        tradingview_mtf_timeframes=("5", "60"))
    now = 1_000_000.0
    up = types.SimpleNamespace(direction="UP", strength=0.8)
    eng.tradingview = types.SimpleNamespace(latest_by_tf={
        ("BTCUSD", "5"): (up, now - 10),      # fresh 5m -> votes
        ("BTCUSD", "60"): (up, now - 2700),   # 45-min-stale 1h (off-grid) -> dropped by the cap
    })
    views = eng._tv_per_tf_views(now, symbol="BTCUSD")
    assert "tv_5m" in views and views["tv_5m"] == 0.9
    assert "tv_60m" not in views                 # stale beyond the 900s window cap


def test_btc_correlated_exposure_sums_directional_and_dep_arb(tmp_path):
    import types
    eng = _mini_engine(tmp_path, correlated_exposure_cap_usd=20.0)
    now = 1_000_000.0
    live, stale = now + 600, now - 600   # window closes in the future vs already closed
    # directional: open UP live ($6), open DOWN live ($5), settled UP (ignored), open-but-STALE UP ($9)
    eng.ledger.positions = {
        "u1": types.SimpleNamespace(status="open", side="up", size_usd=6.0, close_ts=live),
        "d1": types.SimpleNamespace(status="open", side="down", size_usd=5.0, close_ts=live),
        "u2": types.SimpleNamespace(status="settled", side="up", size_usd=9.0, close_ts=live),
        "u3": types.SimpleNamespace(status="open", side="up", size_usd=9.0, close_ts=stale),
    }
    assert eng._btc_correlated_exposure("up", now) == 6.0    # only the live open UP (stale u3 excluded)
    assert eng._btc_correlated_exposure("down", now) == 5.0
    if eng.dep_arb_ledger is not None:             # dep-arb parent-UP adds to BTC-up exposure
        eng.dep_arb_ledger.positions = {
            "p1": {"status": "open", "cost_usd": 5.0, "close_ts": live},
            "p2": {"status": "settled", "cost_usd": 50.0, "close_ts": live},   # ignored (settled)
            "p3": {"status": "open", "cost_usd": 50.0, "close_ts": stale},     # ignored (stuck/stale)
        }
        assert eng._btc_correlated_exposure("up", now) == 11.0   # 6 directional + 5 live dep-arb
        assert eng._btc_correlated_exposure("down", now) == 5.0  # dep-arb never adds to DOWN


def test_engine_status_exposes_council(tmp_path):
    from engine.pulse.engine import PulseEngine, PulseConfig
    from engine.pulse.price import PulsePriceFeed
    from engine.pulse.fair_value import RollingVol

    class _Mkt:
        def active_windows(self, now=None, **kw):
            return []

        def hydrate_books(self, w):
            return w

    feed = PulsePriceFeed(fetcher=lambda: 64000.0, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=2), max_open_lag_s=9999.0)
    cfg = PulseConfig(tick_seconds=1.0, data_dir=str(tmp_path), tradingview_webhook_port=0,
                      llm_council_enabled=True)
    eng = PulseEngine(cfg, market_feed=_Mkt(), price_feed=feed)
    st = eng.status()
    assert st["llm_council"]["enabled"] is True
    assert "members" in st["llm_council"]
    # tick with no windows must not raise (council path guarded / fail-open)
    eng.tick(now=10_000_100.0)
