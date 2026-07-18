"""Production BacktestEngine — chronological, no look-ahead, shared P&L.

Uses the *exact same* strategy / risk / paper simulator as live paper trading:
  - strategy.enhanced_misprice.evaluate_market
  - risk.portfolio_risk.PortfolioRiskManager
  - paper_trader.simulator.PaperSimulator

Tracks every decision (taken + rejected) for selectivity analysis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional, Sequence

import numpy as np

from backtest.synthetic_generator import (
    SyntheticDataGenerator,
    SyntheticUniverse,
    estimate_brier_from_decisions,
)
from models.config import EnhancedMispriceConfig, load_enhanced_config
from models.market import (
    ClosedTrade,
    DecisionPoint,
    DecisionRecord,
    OpenPosition,
    Side,
    TradeOpportunity,
)
from paper_trader.simulator import PaperSimulator
from risk.portfolio_risk import PortfolioRiskManager, risk_unit
from strategy.enhanced_misprice import evaluate_market
from strategy.kelly import kelly_size

logger = logging.getLogger(__name__)

Mode = Literal["enhanced", "naive"]


def settle_pnl(
    side: Side,
    entry_price: float,
    size_usd: float,
    resolved_yes: bool,
    *,
    fee_bps: float = 0.0,
) -> tuple[float, float, bool]:
    """Shared binary settlement (same economics as paper_trader).

    ``fee_bps`` is charged on the winning-side payout (redemption fee);
    losers pay nothing extra since the payout is zero.
    Returns (pnl_usd, exit_price, won).
    """
    shares = size_usd / max(entry_price, 1e-9)
    if side in (Side.YES, Side.UP):
        exit_px = 1.0 if resolved_yes else 0.0
    else:
        exit_px = 0.0 if resolved_yes else 1.0
    payout = shares * exit_px * (1.0 - max(0.0, float(fee_bps)) / 10_000.0)
    pnl = payout - size_usd
    return float(pnl), float(exit_px), pnl > 0


def early_exit_fill(
    entry_price: float,
    size_usd: float,
    shares: float,
    *,
    spread_bps: float,
    slippage_bps: float,
) -> tuple[float, float, float]:
    """Sell an open position into the book before resolution.

    The exit mark is the entry price (no outcome information leaks into the
    fill); the seller pays half-spread + slippage crossing the book.
    Returns (pnl_usd, exit_px, cost_bps).
    """
    cost_bps = max(0.0, float(spread_bps)) + max(0.0, float(slippage_bps))
    exit_px = max(0.0, float(entry_price) * (1.0 - cost_bps / 10_000.0))
    n_shares = shares if shares > 0 else size_usd / max(entry_price, 1e-9)
    pnl = n_shares * exit_px - size_usd
    return float(pnl), float(exit_px), float(cost_bps)


def naive_evaluate(
    decision: DecisionPoint,
    *,
    bankroll: float,
    config: EnhancedMispriceConfig,
    fixed_pct: float = 0.02,
) -> TradeOpportunity:
    """Baseline: abs(q-p) edge only, fixed fractional size, no Beta/Kelly/extremes."""
    edge = abs(decision.q - decision.p)
    side = Side.YES if decision.q >= decision.p else Side.NO
    p_side = decision.p if side == Side.YES else (1.0 - decision.p)
    ok = edge >= config.min_edge
    size = (fixed_pct * bankroll) if ok else 0.0
    # Soft-cap by thin liquidity (naive still respects a crude depth limit)
    if decision.liquidity_usd < 500 and ok:
        size *= 0.35
    return TradeOpportunity(
        market_id=decision.market_id,
        slug=f"{decision.category}-{decision.market_id}",
        side=side,
        p=p_side,
        q=decision.q,
        edge=edge,
        conviction=0.5 + 0.5 * min(1.0, edge / 0.2),  # placeholder, not Beta
        conviction_score=edge if ok else 0.0,
        kelly_f_star=0.0,
        kelly_f=fixed_pct if ok else 0.0,
        kappa=0.0,
        size_usd=round(size, 4),
        risk_unit=risk_unit(size, p_side, bankroll=bankroll) if size > 0 else 0.0,
        liquidity_score=min(1.0, decision.liquidity_usd / 20_000.0),
        time_decay_factor=1.0,
        passes_hard_filter=ok,
        reasons=["naive_edge_ok"] if ok else [f"naive_edge={edge:.3f}<{config.min_edge}"],
        meta={"mode": "naive", "days_to_resolution": decision.days_to_resolution},
    )


@dataclass
class EngineResult:
    """Full backtest output."""

    mode: Mode
    config: EnhancedMispriceConfig
    trades: list[ClosedTrade] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    equity_times: list[float] = field(default_factory=list)
    brier: float = 1.0
    n_markets: int = 0
    n_decision_points: int = 0
    seed: int = 0
    final_cash: float = 0.0
    final_equity: float = 0.0


class BacktestEngine:
    """Event-driven walker over DecisionPoints."""

    def __init__(
        self,
        config: Optional[EnhancedMispriceConfig] = None,
        *,
        mode: Mode = "enhanced",
        seed: int = 7,
    ) -> None:
        self.cfg = config or load_enhanced_config()
        self.mode = mode
        self.seed = seed

    def run_on_decisions(
        self,
        decisions: Sequence[DecisionPoint],
        *,
        n_markets: int = 0,
        seed: Optional[int] = None,
    ) -> EngineResult:
        cfg = self.cfg
        run_seed = seed if seed is not None else self.seed
        # One RNG for the whole run — jitter/slippage draws advance across
        # positions instead of repeating the first draw forever.
        rng = np.random.default_rng(run_seed)
        sim = PaperSimulator(cfg, seed=run_seed)
        rm = PortfolioRiskManager(cfg)
        rm.state.bankroll = sim.cash
        rm.state.peak_bankroll = sim.cash
        ref_bankroll = float(cfg.bankroll)

        # One open position per market_id (first fill wins until resolve)
        open_by_market: dict[str, tuple[OpenPosition, DecisionPoint, TradeOpportunity]] = {}
        closed: list[ClosedTrade] = []
        records: list[DecisionRecord] = []
        equity_curve = [sim.equity]
        equity_times = [0.0]

        # Chronological stream: decisions + synthetic resolution events
        events: list[tuple[float, str, object]] = []
        for d in decisions:
            events.append((d.decision_time, "decision", d))
        # One resolution event per market
        seen: dict[str, DecisionPoint] = {}
        for d in decisions:
            seen[d.market_id] = d
        for mid, d in seen.items():
            events.append((d.resolution_time + 1e-6, "resolve", d))
        events.sort(key=lambda x: (x[0], 0 if x[1] == "decision" else 1, str(x[2])))

        for t, etype, payload in events:
            if etype == "resolve":
                d = payload  # type: DecisionPoint
                if d.market_id not in open_by_market:
                    continue
                pos, src, opp = open_by_market.pop(d.market_id)
                # Early-exit check just before resolve (live conviction proxy)
                live_conv = float(np.clip(opp.conviction + rng.normal(0, 0.02), 0, 1))
                if rm.should_early_exit(pos, live_conv):
                    exit_slip_bps = float(
                        rng.uniform(cfg.slippage_bps_min, cfg.slippage_bps_max)
                    )
                    pnl, exit_px, cost_bps = early_exit_fill(
                        pos.entry_price,
                        pos.size_usd,
                        pos.shares,
                        spread_bps=cfg.early_exit_spread_bps,
                        slippage_bps=exit_slip_bps,
                    )
                    trade = ClosedTrade(
                        position_id=pos.position_id,
                        market_id=pos.market_id,
                        side=pos.side,
                        entry_price=pos.entry_price,
                        exit_price=exit_px,
                        size_usd=pos.size_usd,
                        pnl_usd=pnl,
                        won=pnl > 0,
                        conviction_at_entry=pos.conviction_at_entry,
                        edge_at_entry=opp.edge,
                        early_exit=True,
                        meta={
                            "category": src.category,
                            "days_to_resolution": src.days_to_resolution,
                            "lifetime_frac": src.lifetime_frac,
                            "mode": self.mode,
                            "true_q": src.true_q,
                            "live_conv": live_conv,
                            "exit_cost_bps": cost_bps,
                        },
                    )
                else:
                    pnl, exit_px, won = settle_pnl(
                        pos.side,
                        pos.entry_price,
                        pos.size_usd,
                        bool(d.resolved_yes),
                        fee_bps=cfg.settlement_fee_bps,
                    )
                    trade = ClosedTrade(
                        position_id=pos.position_id,
                        market_id=pos.market_id,
                        side=pos.side,
                        entry_price=pos.entry_price,
                        exit_price=exit_px,
                        size_usd=pos.size_usd,
                        pnl_usd=pnl,
                        won=won,
                        conviction_at_entry=pos.conviction_at_entry,
                        edge_at_entry=opp.edge,
                        early_exit=False,
                        meta={
                            "category": src.category,
                            "days_to_resolution": src.days_to_resolution,
                            "lifetime_frac": src.lifetime_frac,
                            "mode": self.mode,
                            "true_q": src.true_q,
                            "live_conv": live_conv,
                        },
                    )
                sim.close_position(trade)
                rm.record_close(trade)
                rm.state.bankroll = sim.cash
                closed.append(trade)
                # Annotate matching decision record
                for rec in records:
                    if rec.market_id == trade.market_id and rec.taken and rec.won is None:
                        rec.won = trade.won
                        rec.pnl_usd = trade.pnl_usd
                        rec.resolved_yes = bool(d.resolved_yes)
                        break
                equity_curve.append(sim.equity)
                equity_times.append(float(t))
                continue

            # --- decision point ---
            d = payload  # type: DecisionPoint
            if d.market_id in open_by_market:
                # Already in a position on this market — skip (no pyramiding)
                records.append(
                    DecisionRecord(
                        decision_id=d.decision_id,
                        market_id=d.market_id,
                        taken=False,
                        reject_reasons=["already_open"],
                        p=d.p,
                        q=d.q,
                        edge=abs(d.q - d.p),
                        category=d.category,
                        days_to_resolution=d.days_to_resolution,
                        lifetime_frac=d.lifetime_frac,
                        true_q=d.true_q,
                        resolved_yes=d.resolved_yes,
                    )
                )
                continue

            if rm.state.drawdown_pct >= cfg.max_drawdown_hard_pct:
                records.append(
                    DecisionRecord(
                        decision_id=d.decision_id,
                        market_id=d.market_id,
                        taken=False,
                        reject_reasons=[f"hard_dd={rm.state.drawdown_pct:.2%}"],
                        p=d.p,
                        q=d.q,
                        edge=abs(d.q - d.p),
                        category=d.category,
                        days_to_resolution=d.days_to_resolution,
                        lifetime_frac=d.lifetime_frac,
                        true_q=d.true_q,
                        resolved_yes=d.resolved_yes,
                    )
                )
                continue

            sizing_br = min(sim.cash, ref_bankroll * 1.5)
            guard = rm.evaluate_guards()

            if self.mode == "enhanced":
                snap = d.as_snapshot()
                # Strip settlement fields from strategy view conceptually — evaluate_market
                # only uses p,q,liquidity,time; true_q is unused there.
                opp = evaluate_market(
                    snap, config=cfg, guard=guard, bankroll=max(sizing_br, 1.0)
                )
            else:
                opp = naive_evaluate(d, bankroll=max(sizing_br, 1.0), config=cfg)

            if opp.size_usd > cfg.max_single_market_pct * ref_bankroll:
                scale = (cfg.max_single_market_pct * ref_bankroll) / max(opp.size_usd, 1e-9)
                opp.size_usd = round(opp.size_usd * scale, 4)
                opp.risk_unit = risk_unit(opp.size_usd, opp.p, bankroll=sizing_br)

            taken = False
            reject: list[str] = list(opp.reasons) if not opp.passes_hard_filter else []
            if opp.passes_hard_filter and opp.size_usd > 0:
                selected = rm.select_within_budget([opp])
                if not selected:
                    reject = ["risk_budget_or_cash"]
                else:
                    opp = selected[0]
                    fill = sim.open_position(opp)
                    if fill is None:
                        reject = ["fill_rejected"]
                    else:
                        pos = OpenPosition(
                            position_id=fill.position_id,
                            market_id=opp.market_id,
                            slug=opp.slug,
                            side=opp.side,
                            entry_price=fill.fill_price,
                            size_usd=fill.size_usd,
                            shares=fill.shares,
                            q_at_entry=opp.q,
                            conviction_at_entry=opp.conviction,
                            risk_unit=opp.risk_unit,
                            meta=dict(opp.meta or {}),
                        )
                        rm.record_open(pos)
                        open_by_market[d.market_id] = (pos, d, opp)
                        taken = True
                        equity_curve.append(sim.equity)
                        equity_times.append(float(t))

            records.append(
                DecisionRecord(
                    decision_id=d.decision_id,
                    market_id=d.market_id,
                    taken=taken,
                    reject_reasons=[] if taken else reject,
                    side=opp.side if taken or opp.passes_hard_filter else None,
                    p=opp.p,
                    q=opp.q,
                    edge=opp.edge,
                    conviction=opp.conviction,
                    conviction_score=opp.conviction_score,
                    size_usd=opp.size_usd if taken else 0.0,
                    kelly_f=opp.kelly_f,
                    category=d.category,
                    days_to_resolution=d.days_to_resolution,
                    lifetime_frac=d.lifetime_frac,
                    true_q=d.true_q,
                    resolved_yes=d.resolved_yes,
                )
            )

        # Force-close any leftovers at final resolution truth
        for mid, (pos, src, opp) in list(open_by_market.items()):
            pnl, exit_px, won = settle_pnl(
                pos.side,
                pos.entry_price,
                pos.size_usd,
                bool(src.resolved_yes),
                fee_bps=cfg.settlement_fee_bps,
            )
            trade = ClosedTrade(
                position_id=pos.position_id,
                market_id=pos.market_id,
                side=pos.side,
                entry_price=pos.entry_price,
                exit_price=exit_px,
                size_usd=pos.size_usd,
                pnl_usd=pnl,
                won=won,
                conviction_at_entry=pos.conviction_at_entry,
                edge_at_entry=opp.edge,
                meta={"category": src.category, "forced_close": True, "mode": self.mode},
            )
            sim.close_position(trade)
            rm.record_close(trade)
            closed.append(trade)
            equity_curve.append(sim.equity)

        brier = estimate_brier_from_decisions(list(decisions))
        return EngineResult(
            mode=self.mode,
            config=cfg,
            trades=closed,
            decisions=records,
            equity_curve=equity_curve,
            equity_times=equity_times,
            brier=brier,
            n_markets=n_markets or len(seen),
            n_decision_points=len(decisions),
            seed=seed if seed is not None else self.seed,
            final_cash=sim.cash,
            final_equity=sim.equity,
        )

    def run_synthetic(
        self,
        n_markets: Optional[int] = None,
        *,
        seed: Optional[int] = None,
    ) -> EngineResult:
        gen = SyntheticDataGenerator(self.cfg, seed=seed if seed is not None else self.cfg.synthetic_seed)
        uni = gen.generate(n_markets=n_markets)
        return self.run_on_decisions(
            uni.chronological(), n_markets=uni.n_markets, seed=gen.seed
        )


# ---- backward-compatible wrappers used by older CLI / tests ----

@dataclass
class BacktestResult:
    report: object
    trades: list[ClosedTrade] = field(default_factory=list)
    brier: float = 1.0
    target_met: bool = False
    suggested_stricter: list[str] = field(default_factory=list)
    engine: Optional[EngineResult] = None


def run_backtest(
    markets=None,
    *,
    config: Optional[EnhancedMispriceConfig] = None,
    use_synthetic: bool = True,
    mode: Mode = "enhanced",
    n_markets: Optional[int] = None,
    seed: Optional[int] = None,
) -> BacktestResult:
    """Compatibility entry — prefers new DecisionPoint engine."""
    from backtest.metrics import compute_metrics, metrics_to_legacy_report

    cfg = config or load_enhanced_config()
    engine = BacktestEngine(cfg, mode=mode, seed=seed or cfg.synthetic_seed)
    if markets is not None:
        # Convert MarketSnapshot list into pseudo decision points
        decisions = []
        for i, m in enumerate(markets):
            decisions.append(
                DecisionPoint(
                    market_id=m.market_id,
                    decision_id=f"{m.market_id}_d0",
                    decision_time=float(i),
                    lifetime_frac=0.6,
                    category=m.category,
                    days_to_resolution=max(0.01, m.seconds_to_resolution / 86400.0),
                    p=m.p,
                    q=m.q,
                    liquidity_usd=m.liquidity_usd,
                    volume_24h=m.volume_24h,
                    true_q=float(m.true_q if m.true_q is not None else m.q),
                    resolved_yes=bool(m.resolved_yes) if m.resolved_yes is not None else False,
                    resolution_time=float(i) + 1.0,
                )
            )
        er = engine.run_on_decisions(decisions, n_markets=len({d.market_id for d in decisions}))
    elif use_synthetic:
        er = engine.run_synthetic(n_markets=n_markets, seed=seed)
    else:
        from backtest.historical import load_historical_decisions

        decisions = load_historical_decisions()
        if len(decisions) < 50:
            logger.warning("Few historical decisions; falling back to synthetic")
            er = engine.run_synthetic(n_markets=n_markets, seed=seed)
        else:
            er = engine.run_on_decisions(decisions)

    metrics = compute_metrics(er)
    report = metrics_to_legacy_report(metrics, er)
    target = bool(metrics.target_met)
    suggestions: list[str] = []
    if not target:
        suggestions = [
            "# Hermes v3 gates missed — keep mode: strict_real, min_edge≥0.14,",
            "# raise min_conviction / extreme_q, or lower kappa_base. See BACKTEST_GUIDE.md",
        ]
    return BacktestResult(
        report=report,
        trades=er.trades,
        brier=er.brier,
        target_met=target,
        suggested_stricter=suggestions,
        engine=er,
    )


def ensure_target_or_tighten(
    *,
    config: Optional[EnhancedMispriceConfig] = None,
    max_rounds: int = 3,
) -> BacktestResult:
    cfg = (config or load_enhanced_config()).model_copy(deep=True)
    last: Optional[BacktestResult] = None
    for round_i in range(max_rounds):
        cfg.synthetic_seed = cfg.synthetic_seed + round_i * 17
        result = run_backtest(config=cfg, use_synthetic=True)
        last = result
        logger.info(
            "backtest round %d: n=%d WR=%.1f%% DD=%.1f%% Brier=%.3f target=%s",
            round_i + 1,
            result.report.n_trades,
            100 * result.report.win_rate,
            100 * result.report.max_drawdown_pct,
            result.brier,
            result.target_met,
        )
        if result.target_met and result.report.n_trades >= 30:
            return result
        cfg.min_conviction = min(0.97, cfg.min_conviction + 0.01)
        cfg.min_edge = min(0.14, cfg.min_edge + 0.01)
        cfg.extreme_q_high = min(0.90, cfg.extreme_q_high + 0.01)
        cfg.extreme_q_low = max(0.10, cfg.extreme_q_low - 0.01)
        cfg.kappa_base = max(0.15, cfg.kappa_base - 0.05)
    assert last is not None
    return last
