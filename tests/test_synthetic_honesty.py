"""Task 1 — the synthetic harness must be structurally unable to flatter the model.

Old design: q := true_q + small_noise, p := true_q + bigger_noise, which
hard-codes "model beats market". These tests pin the honest design:

  * outcomes come from simulated price paths (close vs strike), not from a
    Bernoulli draw the model gets to peek at;
  * model q is produced by running the SAME live pipeline
    (strategy.advanced_signals.ensemble_cex_implied_up) on those paths;
  * NULL-EDGE GUARDRAIL: when the ensemble is fed pure noise (decoy paths
    carrying zero information about the real outcome), backtest WR after
    costs must collapse to ~coinflip or worse. If the model still "wins",
    the harness is circular and every downstream number is a lie.
"""

from __future__ import annotations

import numpy as np

from backtest import synthetic_generator as sg
from backtest.engine import BacktestEngine
from models.config import load_enhanced_config
from strategy.advanced_signals import AdvancedSignalResult


def test_model_q_comes_from_live_ensemble(monkeypatch):
    """Pin the ensemble to a constant → every q must be that constant.

    If q were still derived from true_q this is impossible, since true_q
    varies across markets.
    """
    calls = {"n": 0}

    def pinned_ensemble(**kwargs):
        calls["n"] += 1
        return AdvancedSignalResult(q=0.5)

    monkeypatch.setattr(sg, "ensemble_cex_implied_up", pinned_ensemble)
    uni = sg.SyntheticDataGenerator(seed=3).generate(n_markets=200)
    assert len(uni.decisions) > 0
    assert calls["n"] == len(uni.decisions)
    assert all(d.q == 0.5 for d in uni.decisions)
    # true_q still varies — so q cannot have been a function of it
    assert len({round(d.true_q, 3) for d in uni.decisions}) > 10


def test_outcome_derived_from_price_path():
    uni = sg.SyntheticDataGenerator(seed=4).generate(n_markets=200)
    assert uni.markets_meta
    for m in uni.markets_meta:
        assert m["resolved_yes"] == (m["close"] > m["strike"]), (
            "resolution must be the simulated path crossing its strike, "
            "not an independent Bernoulli(true_q) draw"
        )


def test_universe_plumbing_sane():
    cfg = load_enhanced_config()
    uni = sg.SyntheticDataGenerator(cfg, seed=6).generate(n_markets=200)
    assert uni.n_markets == 200
    assert len(uni.decisions) == 200 * len(cfg.decision_fracs)
    chrono = uni.chronological()
    assert all(
        chrono[i].decision_time <= chrono[i + 1].decision_time
        for i in range(len(chrono) - 1)
    )
    for d in uni.decisions:
        assert 0.0 < d.p < 1.0 and 0.0 < d.q < 1.0
        assert np.isfinite(d.q) and np.isfinite(d.p)
        assert d.days_to_resolution > 0
        assert d.meta.get("q_source") in {"live_ensemble", "null_noise"}


def test_null_edge_collapses_to_coinflip():
    """THE guardrail. Noise in → no skill out, after costs.

    The decoy path fed to the ensemble is statistically identical to a real
    one but independent of the path that decides the outcome, so q carries
    zero information. Any WR meaningfully above coinflip here means the
    harness still leaks the answer to the model.
    """
    cfg = load_enhanced_config()  # strict_real — the live production profile
    qs: list[float] = []
    resid: list[float] = []
    ps: list[float] = []
    trades = []
    for seed in (5, 6, 7):
        uni = sg.SyntheticDataGenerator(cfg, seed=seed).generate(
            n_markets=600, null_edge=True
        )
        for d in uni.decisions:
            qs.append(d.q)
            ps.append(d.p)
            resid.append((1.0 if d.resolved_yes else 0.0) - d.p)
        er = BacktestEngine(cfg, mode="enhanced", seed=seed).run_on_decisions(
            uni.chronological(), n_markets=uni.n_markets, seed=seed
        )
        trades.extend(er.trades)

    # 1) Information test (the teeth): paper-bet every decision where the
    # model disagrees with the market by >= 0.14 and measure the mean excess
    # win frequency over the price paid. A null model must NOT beat its
    # prices (observed ~ -2pp; sd ~ 0.6pp at this n). The old circular
    # harness (q = true_q + noise) scores ~ +10pp on this statistic.
    bets = []
    for q, r_, p in zip(qs, resid, ps):
        if abs(q - p) < 0.14:
            continue
        side_up = q > p
        price = p if side_up else 1.0 - p
        y = r_ + p  # y - p + p
        won = y if side_up else 1.0 - y
        bets.append(won - price)
    assert len(bets) >= 1000, f"only {len(bets)} paper bets — statistic underpowered"
    excess = float(np.mean(bets))
    assert excess <= 0.02, (
        f"NULL-EDGE VIOLATION: noise-fed q beats its prices by {excess:+.3f} "
        f"over {len(bets)} bets — the harness is circular"
    )

    # 2) The harness must actually LET the null model trade (a gate that takes
    # zero trades proves nothing). Floor lowered 30→12 when the cheap-fade
    # block (side<=0.25 hard-blocked, 2.8% WR live) roughly halved synthetic
    # throughput — the teeth of this test are the information statistic above,
    # not the trade count.
    assert len(trades) >= 12, (
        f"null-edge run produced only {len(trades)} trades — harness cannot "
        "demonstrate the null collapses; widen the universe"
    )

    # 3) Engine-level smoke bounds. Longshot payouts + block correlation make
    # WR noisy at this n, so bounds are loose — the corr test above is exact.
    wr = sum(1 for t in trades if t.won) / len(trades)
    breakeven = float(np.mean([t.entry_price for t in trades]))
    assert wr <= breakeven + 0.15, (
        f"NULL-EDGE VIOLATION: noise model wins {wr:.1%} vs breakeven "
        f"{breakeven:.1%} — far beyond luck"
    )
    total_pnl = float(sum(t.pnl_usd for t in trades))
    assert total_pnl <= 2.0 * cfg.bankroll, (
        f"NULL-EDGE VIOLATION: noise model made ${total_pnl:.2f} after costs"
    )
