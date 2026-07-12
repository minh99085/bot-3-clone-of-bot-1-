"""Execution-realistic edge + KL observe-only feature tests."""

from __future__ import annotations

from engine.pulse.execution_realistic import (
    aggregate_report,
    compute_candidate_edge,
    high_entry_margin_reject,
    kl_model_vs_market,
    max_acceptable_leg2_ask,
    simulate_buy_both_sequential,
    simplex_diagnostics,
)
from engine.pulse.markets import OrderBook


def test_kl_model_vs_market_symmetric_at_equal():
    assert kl_model_vs_market(0.5, 0.5) == 0.0


def test_kl_model_vs_market_positive_when_diverges():
    kl = kl_model_vs_market(0.7, 0.5)
    assert kl is not None and kl > 0


def test_simplex_diagnostics_buy_both_signal():
    up = OrderBook(best_bid=0.48, best_ask=0.49, asks=[(0.49, 1000.0)], bids=[(0.48, 1000.0)])
    dn = OrderBook(best_bid=0.48, best_ask=0.49, asks=[(0.49, 1000.0)], bids=[(0.48, 1000.0)])
    sx = simplex_diagnostics(up, dn, 10.0)
    assert sx["abs_tob_ask_residual"] is not None
    assert sx["buy_both_arb_signal"] is True


def test_high_entry_margin_reject_blocks_expensive_low_margin():
    assert high_entry_margin_reject(ask=0.85, calibrated_prob=0.86, min_margin=0.04) == (
        "high_entry_insufficient_margin"
    )
    assert high_entry_margin_reject(ask=0.85, calibrated_prob=0.92, min_margin=0.04) is None


def test_compute_candidate_edge_fields():
    book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=500,
                     asks=[(0.55, 1000.0)], bids=[(0.50, 1000.0)])
    block = compute_candidate_edge(
        side="up", raw_fair_p=0.6, calibrated_fair_p=0.58, market_price=0.54,
        outcome_prob=0.58, book=book, size_usd=10.0, up_book=book, down_book=book)
    assert block["vwap_entry_price"] is not None
    assert block["execution_realistic_ev"] is not None
    assert block["kl_model_vs_market"] is not None
    assert "simplex" in block


def test_pre_commit_leg2_max():
    assert max_acceptable_leg2_ask(leg1_vwap=0.30, epsilon=0.05) == 0.65


def test_sequential_sim_rejects_bible_slippage():
    up = OrderBook(best_bid=0.28, best_ask=0.30, asks=[(0.30, 10_000.0)], bids=[(0.28, 10_000.0)])
    dn = OrderBook(best_bid=0.43, best_ask=0.45, asks=[(0.45, 10_000.0)], bids=[(0.43, 10_000.0)])
    sim = simulate_buy_both_sequential(
        up, dn, target_usd=50.0, epsilon=0.05, leg2_slippage_bps=8000.0)
    assert sim["non_atomic_pass"] is False
    assert sim.get("unwind_required") is True


def test_aggregate_report_rollup():
    rep = aggregate_report(
        samples=[{"execution_realistic_ev": 0.5, "kl_model_vs_market": 0.1}],
        payoff_guards={"rejected_bad_reward_to_risk": 2},
        kl_aggregate={"observe_only": True},
    )
    assert rep["candidates_scored"] == 1
    assert rep["payoff_guards"]["rejected_bad_reward_to_risk"] == 2
    assert rep["observe_only"] is True