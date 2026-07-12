"""Learned Selectivity Gate v1 — reject proven-losing buckets using live evidence (PAPER ONLY).

Proves: losing bucket rejected; profitable/aligned bucket passes; exploration is separated from
headline metrics and capped; calibration differs from raw when evidence exists; counterfactual
replay works; TradingView cannot bypass the gate; paper-only/live-disabled enforced; reconciliation
still passes.
"""

from __future__ import annotations

from engine.pulse.selectivity import (SelectivityEvidence, LearnedSelectivityGate, calibrate_fair,
                                       calibrate_chosen_prob)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


def _evidence_with(dim, bucket, *, n, win_rate, avg_pnl, up_rate=0.5):
    ev = SelectivityEvidence()
    wins = int(round(n * win_rate))
    for i in range(n):
        won = i < wins
        up = i < int(round(n * up_rate))
        # construct PnL so the sign matches avg_pnl while wins/losses are consistent
        pnl = (2.0 if won else -5.0)
        ev.record({dim: bucket}, won=won, pnl=pnl, ev_after_cost=(avg_pnl), outcome_up=up)
    return ev


# ------------------------------- gate accept/reject (req #2,#3,#4) ------------------------- #
def test_losing_bucket_rejected_winning_passes():
    gate = LearnedSelectivityGate(min_samples=30, min_win_rate=0.52, exploration_rate=0.0)
    losing = _evidence_with("zscore_bucket", "-1..1", n=40, win_rate=0.40, avg_pnl=-0.02)
    r = gate.evaluate({"zscore_bucket": "-1..1"}, losing)
    assert r["decision"] == "reject" and r["reasons"][0].startswith("bad_bucket:zscore_bucket=-1..1")
    # a winning bucket with enough samples passes
    winning = _evidence_with("zscore_bucket", "<=-2", n=40, win_rate=0.75, avg_pnl=0.05)
    assert gate.evaluate({"zscore_bucket": "<=-2"}, winning)["decision"] == "accept"
    # below sample threshold -> cannot judge -> accept (cold start)
    thin = _evidence_with("zscore_bucket", "1..2", n=5, win_rate=0.0, avg_pnl=-0.05)
    assert gate.evaluate({"zscore_bucket": "1..2"}, thin)["decision"] == "accept"


def test_specific_bad_buckets_guarded():
    gate = LearnedSelectivityGate(min_samples=30, exploration_rate=0.0)
    for dim, bucket in (("stale_divergence", "stale_polymarket_up"), ("zscore_bucket", "-1..1"),
                        ("ttc_bucket", "120-240s"), ("confidence_tier", "medium")):
        ev = _evidence_with(dim, bucket, n=40, win_rate=0.35, avg_pnl=-0.03)
        assert gate.evaluate({dim: bucket}, ev)["decision"] == "reject", (dim, bucket)


def test_breakeven_confidence_does_not_block_marginal_coinflip_bucket():
    """Regression for the overblocking bug: a dominant near-breakeven bucket (e.g. trending at 52.5%
    over ~200 trades, avg_win 3.75 / avg_loss 5.0 -> breakeven 0.571) is NOT confidently below its
    breakeven, so it must NOT be hard-vetoed even though cumulative PnL is slightly negative."""
    ev = SelectivityEvidence()
    n, wins = 198, 104                                  # win_rate ~0.525, like the live trending bucket
    for i in range(n):
        won = i < wins
        ev.record({"hurst_regime": "trending"}, won=won, pnl=(3.747 if won else -5.0),
                  outcome_up=won)
    gate = LearnedSelectivityGate(min_samples=30, exploration_rate=0.0, confidence_z=1.64)
    res = gate.evaluate({"hurst_regime": "trending"}, ev)
    assert res["decision"] == "accept", res            # coin-flip near breakeven -> not blocked
    # but a genuinely, confidently losing bucket (35% over 60) IS blocked
    ev2 = SelectivityEvidence()
    for i in range(60):
        won = i < 21                                    # 35% win rate
        ev2.record({"hurst_regime": "trending"}, won=won, pnl=(3.747 if won else -5.0),
                   outcome_up=won)
    r2 = gate.evaluate({"hurst_regime": "trending"}, ev2)
    assert r2["decision"] == "reject" and r2["bad_buckets"][0]["confidently_losing"] is True
    assert r2["bad_buckets"][0]["breakeven_win_rate"] > 0.5


def test_bucket_evidence_is_auditable():
    ev = _evidence_with("hurst_regime", "trending", n=198, win_rate=0.525, avg_pnl=-0.4)
    gate = LearnedSelectivityGate(min_samples=30)
    be = gate.bucket_evidence(ev)
    assert be["buckets"], be
    row = be["buckets"][0]
    for k in ("dimension", "bucket", "n", "win_rate", "breakeven_win_rate", "win_rate_upper_ci",
              "ev_per_trade", "confidently_losing"):
        assert k in row, k


# ------------------------------- exploration separation + cap (req #5) --------------------- #
def test_exploration_separated_and_capped():
    # exploration_rate is hard-capped at 0.05 even if a larger value is requested
    g = LearnedSelectivityGate(min_samples=30, exploration_rate=0.5, seed=7)
    assert g.exploration_rate == 0.05
    ev = _evidence_with("direction", "up", n=40, win_rate=0.30, avg_pnl=-0.05)
    rej = exp = 0
    for _ in range(2000):
        d = g.evaluate({"direction": "up"}, ev)["decision"]
        rej += int(d == "reject")
        exp += int(d == "explore")
    assert exp > 0 and rej > 0
    assert 0.02 < exp / (exp + rej) < 0.08          # ~5% exploration
    # per-decision settled stats keep exploration separate from passed (headline)
    g.record_settled("passed", won=True, pnl=2.0)
    g.record_settled("explored", won=False, pnl=-5.0)
    rep = g.report()
    assert rep["pnl_by_gate_decision"]["passed"]["pnl_usd"] == 2.0
    assert rep["pnl_by_gate_decision"]["explored"]["pnl_usd"] == -5.0
    assert rep["explored"] == exp and rep["rejected"] == rej


# ------------------------------- calibration (req #6) -------------------------------------- #
def test_calibrated_differs_from_raw_with_evidence():
    ev = _evidence_with("direction", "up", n=40, win_rate=0.5, up_rate=0.0, avg_pnl=0.0)  # up_rate 0
    raw, cal, diag = calibrate_fair(0.70, {"direction": "up"}, ev, min_samples=30, max_shrink=0.5)
    assert raw == 0.70 and cal < raw and diag is not None and diag["empirical_up_rate"] == 0.0
    # no evidence -> calibrated == raw
    raw2, cal2, diag2 = calibrate_fair(0.70, {"direction": "up"}, SelectivityEvidence(),
                                       min_samples=30)
    assert raw2 == cal2 == 0.70 and diag2 is None


def test_calibrate_chosen_prob_shrinks_overconfident_toward_realized():
    """The negative-expectancy fix: a model that CLAIMS p_win=0.70 in a bucket whose realized
    win-rate is 0.50 must be shrunk toward 0.50 before the EV gate, so an over-priced favourite
    fails the EV floor. Calibration is symmetric (a genuinely winning bucket calibrates UP)."""
    ev = _evidence_with("markov_state", "chop_noise", n=60, win_rate=0.50, avg_pnl=-0.02)
    raw, cal, diag = calibrate_chosen_prob(0.70, {"markov_state": "chop_noise"}, ev,
                                           min_samples=30, max_shrink=0.6)
    assert raw == 0.70 and cal < raw and 0.50 <= cal < 0.70
    assert diag is not None and diag["empirical_win_rate"] == 0.5
    # genuinely winning bucket: a conservative claim calibrates UP toward realized 0.70
    win = _evidence_with("markov_state", "trending", n=60, win_rate=0.70, avg_pnl=0.05)
    _, cal_up, _ = calibrate_chosen_prob(0.55, {"markov_state": "trending"}, win,
                                         min_samples=30, max_shrink=0.6)
    assert cal_up > 0.55


def test_calibrate_chosen_prob_coldstart_untouched():
    # below min_samples -> no calibration (unproven contexts still get explored with the raw prob)
    thin = _evidence_with("markov_state", "new", n=5, win_rate=0.2, avg_pnl=-0.05)
    raw, cal, diag = calibrate_chosen_prob(0.70, {"markov_state": "new"}, thin, min_samples=30)
    assert raw == cal == 0.70 and diag is None
    # None passes through safely
    assert calibrate_chosen_prob(None, {}, SelectivityEvidence()) == (None, None, None)


# ------------------------------- counterfactual replay (req #7) ---------------------------- #
def test_counterfactual_replay():
    gate = LearnedSelectivityGate(min_samples=30, min_win_rate=0.52, exploration_rate=0.0)
    ev = SelectivityEvidence()
    positions = []
    for i in range(40):                         # losing 'up' bucket
        ev.record({"direction": "up"}, won=False, pnl=-5.0, outcome_up=False)
        positions.append({"tags": {"direction": "up"}, "won": False, "pnl": -5.0})
    for i in range(10):                         # winning 'down' bucket (too few to be 'bad')
        ev.record({"direction": "down"}, won=True, pnl=2.0, outcome_up=False)
        positions.append({"tags": {"direction": "down"}, "won": True, "pnl": 2.0})
    cf = gate.counterfactual_replay(ev, positions)
    assert cf["replayed"] == 50 and cf["trades_rejected"] == 40 and cf["losses_avoided"] == 40
    assert cf["counterfactual_trades"] == 10 and cf["counterfactual_win_rate"] == 1.0
    assert cf["counterfactual_pnl_usd"] == 20.0
    assert cf["baseline_win_rate"] == 0.2 and cf["baseline_pnl_usd"] == -180.0
    assert cf["counterfactual_pnl_usd"] > cf["baseline_pnl_usd"]


# ============================ engine end-to-end =========================================== #
class _Mkt:
    def __init__(self, w, *, deep):
        self._w, self._deep = w, deep

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        if self._deep:
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=50000,
                                  bid_depth_usd=50000, asks=[(0.55, 100000.0)],
                                  bids=[(0.50, 100000.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=49000,
                                    bid_depth_usd=44000, asks=[(0.49, 100000.0)],
                                    bids=[(0.44, 100000.0)])
        else:
            w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=2.0, bid_depth_usd=2.0,
                                  asks=[(0.55, 1.0)], bids=[(0.50, 1.0)])
            w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=2.0, bid_depth_usd=2.0,
                                    asks=[(0.49, 1.0)], bids=[(0.44, 1.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _engine(tmp_path, *, deep=True, expl=0.0, **over):
    t0 = 9_960_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      selectivity_exploration_rate=expl, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False, data_dir=str(tmp_path), **over)
    return PulseEngine(cfg, market_feed=_Mkt(win, deep=deep), price_feed=feed), t0


def _drive(eng, t0):
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)


def test_engine_rejects_proven_losing_bucket(tmp_path):
    eng, t0 = _engine(tmp_path, deep=True, expl=0.0, selectivity_min_samples=30)
    # pre-seed live evidence: the 'up' direction bucket is a proven loser (rising price -> bot wants UP)
    for _ in range(40):
        eng.selectivity_evidence.record({"direction": "up"}, won=False, pnl=-5.0, outcome_up=False)
    _drive(eng, t0)
    assert eng.ledger.trades == 0                        # gate rejected the losing-bucket candidate
    lc = eng.status()["decision_lifecycle"]
    assert lc["rejected_by_stage"].get("selectivity_gate", 0) >= 1
    sg = eng.status()["learned_selectivity_gate"]
    assert sg["enabled"] is True and sg["rejected"] >= 1
    assert any("direction=up" in k for k in sg["reject_reasons"])
    assert eng.light_report()["global_reconciled"] is True
    assert eng.status()["live_trading_enabled"] is False and eng.status()["paper_only"] is True


def test_engine_passes_when_no_bad_evidence(tmp_path):
    eng, t0 = _engine(tmp_path, deep=True, expl=0.0)     # cold start, no evidence -> accepts
    _drive(eng, t0)
    assert eng.ledger.trades >= 1                         # trades normally when nothing is proven bad
    sg = eng.status()["learned_selectivity_gate"]
    assert sg["accepted"] >= 1
    # the candidate carries raw + calibrated fair (calibration reported)
    acc = [r for r in eng.status()["recent_evaluations"] if r["terminal"] == "accepted"]
    assert acc and "raw_fair_p_up" in acc[0]["calibration"] and "calibrated_fair_p_up" in acc[0]["calibration"]
    # the EV-gate probability calibration is also reported (the negative-expectancy fix)
    assert "raw_outcome_prob" in acc[0]["calibration"] and "calibrated_outcome_prob" in acc[0]["calibration"]
    assert eng.light_report()["global_reconciled"] is True


def test_engine_ev_floor_rejects_overconfident_in_proven_flat_bucket(tmp_path):
    """Calibration + EV floor: when a bucket is proven coin-flip (win-rate ~0.50) but the model
    still wants to buy the favourite near 0.55, the calibrated EV (~0.50-0.55<0) fails the 0.02
    floor, so the trade is rejected by the execution gate rather than bleeding negative expectancy."""
    eng, t0 = _engine(tmp_path, deep=True, expl=0.0, selectivity_min_samples=30,
                      exec_min_ev_after_slippage=0.02, calibration_min_samples=30,
                      calibration_max_shrink=0.6)
    # proven coin-flip across the markov bucket the rising-price candidate will land in -> calibrate
    # the model's (overconfident) prob down toward 0.50; coin-flip is NOT hard-blocked by selectivity.
    for st in ("stale_polymarket_up", "stale_polymarket_down", "chop_noise", "trending_up",
               "trending_down"):
        for _ in range(40):
            eng.selectivity_evidence.record({"markov_state": st}, won=False, pnl=-5.0,
                                            outcome_up=False)
            eng.selectivity_evidence.record({"markov_state": st}, won=True, pnl=4.5, outcome_up=True)
    _drive(eng, t0)
    lc = eng.status()["decision_lifecycle"]
    rejected = lc["rejected_by_stage"]
    assert (rejected.get("execution_gate", 0) + rejected.get("selectivity_gate", 0)) >= 1
    assert eng.light_report()["global_reconciled"] is True
    assert eng.status()["paper_only"] is True


def test_research_apply_is_evidence_gated_and_excludes_coarse_dims(tmp_path):
    # self-improving loop is MAKER-CHECKER: a Claude avoid-context becomes a hard block ONLY when the
    # bot's OWN evidence confirms it is confidently losing. 'regime' aliases to 'hurst_regime';
    # 'direction'/'depth_bucket' (coarse/liquidity) and unknown dims are ignored; case normalized.
    eng, _ = _engine(tmp_path, deep=True, expl=0.0, selectivity_min_samples=30)
    # seed live evidence: hurst_regime=trending is CONFIDENTLY losing (35% win over 40)
    for i in range(40):
        won = i < 14
        eng.selectivity_evidence.record({"hurst_regime": "trending"}, won=won,
                                        pnl=(3.0 if won else -5.0), outcome_up=won)
    eng._research_apply({"avoid_contexts": ["regime=Trending", "direction=UP", "bogus=zzz",
                                            "depth_bucket=>=1000", "markov_state=chop_noise"]})
    assert "hurst_regime=trending" in eng._research_avoid     # evidence-backed -> applied
    assert "markov_state=chop_noise" not in eng._research_avoid  # NO evidence -> NOT applied (checker)
    assert not any(k.startswith("direction") for k in eng._research_avoid)   # whole side excluded
    assert not any(k.startswith("depth_bucket") for k in eng._research_avoid)  # liquidity attr excluded
    assert not any(k.startswith("bogus") for k in eng._research_avoid)       # unknown dim ignored
    # the matcher hits a flagged context but NEVER blocks on direction
    assert eng._research_avoid_hit({"hurst_regime": "trending", "direction": "up"}) == "hurst_regime=trending"
    assert eng._research_avoid_hit({"direction": "up"}) is None


def test_engine_research_exploit_is_evidence_gated(tmp_path):
    # EXPLOIT side of the self-improving loop (dual of avoid): a Claude exploit-context is promoted
    # ONLY when the bot's own data confirms it is confidently WINNING (Wilson lower > breakeven, +PnL).
    eng, _ = _engine(tmp_path, deep=True, expl=0.0, selectivity_min_samples=30)
    # seed a confidently-WINNING bucket: markov_state=stale_polymarket_down at ~75% over 40
    for i in range(40):
        won = i < 30
        eng.selectivity_evidence.record({"markov_state": "stale_polymarket_down"}, won=won,
                                        pnl=(4.0 if won else -5.0), outcome_up=won)
    # and a LOSING bucket that must NOT be promoted to exploit
    for i in range(40):
        won = i < 14
        eng.selectivity_evidence.record({"markov_state": "chop_noise"}, won=won,
                                        pnl=(4.0 if won else -5.0), outcome_up=won)
    eng._research_apply({"avoid_contexts": [],
                         "exploit_contexts": ["markov_state=stale_polymarket_down",
                                              "markov_state=chop_noise"]})
    assert "markov_state=stale_polymarket_down" in eng._research_exploit   # winning -> promoted
    assert "markov_state=chop_noise" not in eng._research_exploit          # losing -> rejected
    assert eng._research_exploit_hit({"markov_state": "stale_polymarket_down"}) is True
    assert eng._research_exploit_hit({"markov_state": "chop_noise"}) is False


def test_engine_research_avoid_blocks_flagged_context(tmp_path):
    # the self-improving loop end-to-end: a research-flagged context is hard-blocked before execution.
    from engine.pulse.reporting import spread_bucket
    eng, t0 = _engine(tmp_path, deep=True, expl=0.0, research_auto_apply=True)
    # the deep up book has spread 0.05 -> add THAT spread bucket as a research avoid-rule
    eng._research_avoid = {"spread_bucket=%s" % spread_bucket(0.05)}
    _drive(eng, t0)
    lc = eng.status()["decision_lifecycle"]
    assert lc["rejected_by_stage"].get("research_avoid", 0) >= 1
    assert eng.ledger.trades == 0                             # the avoided context never trades
    assert eng.light_report()["global_reconciled"] is True


def test_engine_underdog_floor_blocks_cheap_side(tmp_path):
    # buying below the entry-price floor is rejected on the opinion path. The deep up book fills at
    # 0.55; with the floor set to 0.60 that fill is "underdog" and must be blocked at the exec gate.
    eng, t0 = _engine(tmp_path, deep=True, expl=0.0, min_entry_price=0.60)
    _drive(eng, t0)
    assert eng.ledger.trades == 0
    assert eng.status()["execution_gate"]["rejected"].get("underdog_price_below_floor", 0) >= 1
    assert eng.light_report()["global_reconciled"] is True
    # with a permissive floor (0.0) the same 0.55 fill trades normally
    eng2, t02 = _engine(tmp_path, deep=True, expl=0.0, min_entry_price=0.0)
    _drive(eng2, t02)
    assert eng2.ledger.trades >= 1


def test_engine_selectivity_cannot_help_tradingview_bypass_gate(tmp_path):
    # thin book -> execution gate rejects regardless of selectivity decision
    eng, t0 = _engine(tmp_path, deep=False, expl=0.0, tradingview_secret="s3cr3t",
                      tradingview_webhook_port=0, tradingview_allowed_symbols=("BTC/USD",))
    import json as _json
    eng.tradingview.ingest(_json.dumps({"secret": "s3cr3t", "bot_name": "hermes",
                                        "symbol": "BTC/USD", "direction": "UP",
                                        "event_id": "tv"}).encode(), now=t0 - 6)
    _drive(eng, t0)
    assert eng.ledger.trades == 0                         # gate is the sole execution authority
    assert eng.status()["live_trading_enabled"] is False
    if eng.webhook is not None:
        eng.webhook.stop()
