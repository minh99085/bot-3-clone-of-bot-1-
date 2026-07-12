"""Phase 10 reporting + learning loop — report counts reconcile with the ledger."""

from __future__ import annotations

from engine.pulse.reporting import (spread_bucket, depth_bucket, confidence_tier,
                                     OutcomeGroups, promotion_demotion, build_light_report,
                                     build_report_sections, build_full_report_md,
                                     ledger_stats_by_entry_price, ledger_wr_ev_books)
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


def test_buckets():
    assert spread_bucket(0.005) == "<=0.01" and spread_bucket(0.1) == ">0.06"
    assert depth_bucket(10) == "<50" and depth_bucket(5000) == ">=1000"
    assert confidence_tier(0.1) == "low" and confidence_tier(0.9) == "high"
    assert spread_bucket(None) == "na" and confidence_tier(None) == "na"


def test_ledger_stats_by_entry_price_flb_edge():
    # favorite band resolves better than implied (+edge); longshot worse (-edge)
    positions = {
        "a": {"status": "settled", "entry_price": 0.62, "won": True, "pnl_usd": 0.38},
        "b": {"status": "settled", "entry_price": 0.64, "won": True, "pnl_usd": 0.36},
        "c": {"status": "settled", "entry_price": 0.66, "won": False, "pnl_usd": -0.66},
        "d": {"status": "settled", "entry_price": 0.12, "won": False, "pnl_usd": -0.12},
        "e": {"status": "settled", "entry_price": 0.14, "won": False, "pnl_usd": -0.14},
        "f": {"status": "open", "entry_price": 0.60},          # ignored (not settled)
    }
    out = ledger_stats_by_entry_price(positions)
    fav = out["0.60-0.70"]
    assert fav["n"] == 3 and fav["wins"] == 2 and fav["losses"] == 1
    assert fav["win_rate"] == round(2 / 3, 4)
    assert fav["edge"] > 0                                     # realized (0.667) > implied (~0.64)
    short = out["<0.35"]
    assert short["n"] == 2 and short["win_rate"] == 0.0 and short["edge"] < 0
    assert "0.45-0.50" not in out                              # no trades there -> omitted


def test_ledger_wr_ev_books_split():
    positions = {
        "fav1": {"status": "settled", "entry_price": 0.62, "won": True, "pnl_usd": 3.0,
                 "research": {"gate_decision": "passed"}},
        "fav2": {"status": "settled", "entry_price": 0.60, "won": False, "pnl_usd": -6.0,
                 "research": {"gate_decision": "osmani_verified"}},
        "dog": {"status": "settled", "entry_price": 0.35, "won": False, "pnl_usd": -3.5,
                "research": {"gate_decision": "passed"}},
        "cex": {"status": "settled", "entry_price": 0.65, "won": True, "pnl_usd": 50.0,
                "research": {"gate_decision": "cex_lead"}},
        "open": {"status": "open", "entry_price": 0.70},
    }
    books = ledger_wr_ev_books(positions, wr_entry_floor=0.58)
    assert books["wr_book"]["n"] == 2
    assert books["wr_book"]["wins"] == 1
    assert books["wr_book"]["win_rate"] == 0.5
    assert books["ev_book"]["n"] == 2  # underdog + cex_lead (even at 0.65)
    assert books["combined_settled"] == 4
    assert books["wr_entry_floor"] == 0.58


def test_outcome_groups_multi_dimension():
    g = OutcomeGroups()
    g.record({"hurst_regime": "trending", "ttc_bucket": "120-240s"}, pnl=7.5, won=True,
             fair_at_entry=0.7, outcome_up=True)
    g.record({"hurst_regime": "trending", "ttc_bucket": "60-120s"}, pnl=-5.0, won=False,
             fair_at_entry=0.6, outcome_up=False)
    s = g.summary()
    assert s["hurst_regime"]["trending"]["n"] == 2
    assert s["ttc_bucket"]["120-240s"]["n"] == 1 and s["ttc_bucket"]["60-120s"]["n"] == 1


def test_promotion_demotion_extraction():
    tt = {"table": {"edge_quality:high": {"tier": "A+"}, "regime:trend_up": {"tier": "A"},
                    "edge_quality:low": {"tier": "C"}, "regime:danger": {"tier": "D"}}}
    pd = promotion_demotion(tt)
    assert set(pd["promotion_candidates"]) == {"edge_quality:high", "regime:trend_up"}
    assert set(pd["demotion_candidates"]) == {"edge_quality:low", "regime:danger"}


class _Mkt:
    def __init__(self, w):
        self._w = w
    def active_windows(self, now=None, **kw):
        return [self._w]
    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=550, bid_depth_usd=500,
                              asks=[(0.55, 1000.0)], bids=[(0.50, 1000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=490, bid_depth_usd=440,
                                asks=[(0.49, 1000.0)], bids=[(0.44, 1000.0)])
        return w
    def fetch_resolution(self, market_id):
        return True


def test_light_report_reconciles_with_ledger(tmp_path):
    t0 = 9_300_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    eng = PulseEngine(PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, directional_down_only=False, directional_block_up_until_promoted=False, directional_up_restrictions_enabled=False,
                                  basis_buffer=0.0, min_seconds_since_open=0.0,
                                  sigma_trust_floor=0.0, min_vol_samples=2, settle_grace_s=0.0,
                                  exec_max_depth_consume_frac=0.9,
                                  tv_mtf_conflict_gate_enabled=False,
                                  data_dir=str(tmp_path)),
                      market_feed=_Mkt(win), price_feed=feed)
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(8):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)
    rep = eng.light_report()
    # all required report sections present
    for fld in ("candidate_lifecycle", "execution_stats", "reject_reasons",
                "ev_before_after_costs", "calibration", "edge_model_calibration",
                "sample_sizes", "missing_data_reasons", "confidence_tier_table", "sizing",
                "promotion_candidates", "demotion_candidates", "reconciliation",
                "pnl_by_hurst_regime", "pnl_by_markov_state", "pnl_by_ttc_bucket",
                "pnl_by_spread_bucket", "pnl_by_depth_bucket", "pnl_by_confidence_tier"):
        assert fld in rep, fld
    # report counts reconcile with the ledger (new global reconciliation shape)
    rc = rep["reconciliation"]
    cnt = rc["counts"]
    assert rep["global_reconciled"] is True and rc["global_reconciled"] is True
    assert not rc["failed_checks"]
    assert (cnt["execution_gate_accepted"] == cnt["paper_fills_created"]
            == rep["execution_stats"]["fills"] == eng.ledger.trades)
    assert cnt["settled_trades"] == eng.ledger.settled >= 1
    assert cnt["settled_trades"] + cnt["open_positions"] == cnt["ledger_trades"]
    assert rc["checks"]["lifecycle_internal"]["pass"] is True
    # the settled trade is grouped under its entry-time tags (sum of n == settled)
    total_n = sum(b["n"] for b in rep["pnl_by_markov_state"].values())
    assert total_n == eng.ledger.settled
    assert rep.get("schema") == "btc_pulse_light_report/1.3"
    sec = rep.get("sections") or {}
    assert "trading_performance" in sec and "operation" in sec and "external_signals" in sec
    assert "scores" in rep and "overall" in (rep.get("scores") or {})
    md = build_full_report_md(rep, eng.status(), eng.ledger.to_dict())
    assert "1. Trading Performance" in md and "2. Operation" in md and "3. External Signals" in md

