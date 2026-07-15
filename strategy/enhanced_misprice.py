"""Enhanced misprice strategy — wraps Hermes CEX↔PM detection with:

1. Polymarket Kelly Criterion
2. Bayesian Beta conviction (scipy)
3. Conviction score ranking
4. Portfolio risk budgeting hooks
5. Dynamic DD / WR guards

Does NOT remove ``hermes.mispricing.detect_mispricing`` — it consumes its
output (q ≈ CEX-implied, p ≈ Polymarket) and applies the hard filters above.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from models.config import EnhancedMispriceConfig, load_enhanced_config
from models.market import MarketSnapshot, Side, TradeOpportunity
from risk.portfolio_risk import GuardState, PortfolioRiskManager, risk_unit
from strategy.bayesian import bayesian_conviction, passes_hard_entry_filter
from strategy.kelly import kelly_size
from utils.scoring import conviction_score, liquidity_score, time_decay_factor

logger = logging.getLogger(__name__)


def _side_from_q_p(q: float, p: float) -> Side:
    """Trade YES/UP when model > market; else NO/DOWN."""
    return Side.YES if q >= p else Side.NO


def evaluate_market(
    market: MarketSnapshot,
    *,
    config: Optional[EnhancedMispriceConfig] = None,
    guard: Optional[GuardState] = None,
    bankroll: Optional[float] = None,
) -> TradeOpportunity:
    """Run enhanced filters + Kelly sizing on one market snapshot.

    Parameters
    ----------
    market.p : Polymarket implied P(YES/UP)
    market.q : model fair P(YES/UP) — from CEX mispricing or other pluggable model
    """
    cfg = config or load_enhanced_config()
    kappa = guard.kappa if guard else cfg.kappa_base
    min_conv = guard.min_conviction if guard else cfg.min_conviction
    br = float(bankroll if bankroll is not None else cfg.bankroll)

    side = _side_from_q_p(market.q, market.p)
    # Price paid for the chosen contract
    p_side = market.p if side in (Side.YES, Side.UP) else (1.0 - market.p)

    n_eff = cfg.n_eff.for_category(market.category)
    bayes = bayesian_conviction(market.q, market.p, n_eff, side=side.value)

    ok, fail_reasons = passes_hard_entry_filter(
        market.q,
        market.p,
        bayes.conviction,
        min_edge=cfg.min_edge,
        min_conviction=min_conv,
        extreme_q_high=cfg.extreme_q_high,
        extreme_q_low=cfg.extreme_q_low,
    )

    liq = liquidity_score(market.liquidity_usd, market.volume_24h)
    tdf = time_decay_factor(market.seconds_to_resolution)
    score = conviction_score(market.q, market.p, bayes.conviction, liq, tdf)
    edge = abs(market.q - market.p)

    reasons: list[str] = []
    if guard and guard.guard_active:
        reasons.append(f"guard_active:{guard.reason}")

    kelly = kelly_size(
        q=market.q,
        p=market.p,
        side=side.value,
        bankroll=br,
        kappa=kappa,
        max_pct=cfg.max_single_market_pct,
    )

    size = kelly.size_usd if ok else 0.0
    ru = risk_unit(size, p_side, bankroll=br) if size > 0 else 0.0

    if not ok:
        reasons.extend(fail_reasons)
    else:
        reasons.append(
            f"PASS edge={edge:.3f} conv={bayes.conviction:.3f} "
            f"kelly_f={kelly.f:.4f} size=${size:.2f}"
        )

    return TradeOpportunity(
        market_id=market.market_id,
        slug=market.slug,
        side=side,
        p=p_side,
        q=market.q,
        edge=edge,
        conviction=bayes.conviction,
        conviction_score=score if ok else 0.0,
        kelly_f_star=kelly.f_star,
        kelly_f=kelly.f,
        kappa=kappa,
        size_usd=size,
        risk_unit=ru,
        liquidity_score=liq,
        time_decay_factor=tdf,
        passes_hard_filter=ok,
        reasons=reasons,
        meta={
            "n_eff": n_eff,
            "bayes_alpha": bayes.alpha,
            "bayes_beta": bayes.beta,
            "yes_price": market.p,
            "model_q": market.q,
            "category": market.category,
            "timeframe": market.timeframe,
            "kelly_capped": kelly.capped,
            **(market.meta or {}),
        },
    )


def enhance_from_hermes_mispricing(
    *,
    market_id: str,
    slug: str,
    pm_implied_up: float,
    cex_implied_up: float,
    dislocation: float,
    mp_conviction: float,
    timeframe: str = "5m",
    liquidity_usd: float = 5_000.0,
    volume_24h: float = 10_000.0,
    seconds_to_resolution: float = 300.0,
    chainlink_vs_cex_bps: float = 0.0,
    active: bool = True,
    config: Optional[EnhancedMispriceConfig] = None,
    risk_manager: Optional[PortfolioRiskManager] = None,
    bankroll: Optional[float] = None,
) -> TradeOpportunity:
    """Wrap an existing Hermes ``MispricingSignal`` into enhanced filters.

    q := CEX-implied P(UP)  (lead signal)
    p := Polymarket YES price
    """
    cfg = config or load_enhanced_config()
    rm = risk_manager or PortfolioRiskManager(cfg)
    guard = rm.evaluate_guards()

    # Map CEX-implied + dislocation into a model q suitable for the hard
    # Bayesian filter. Strong dislocations push q toward extremes so crypto
    # HF lead-lag can clear q≥extreme_high / q≤extreme_low.
    q = float(cex_implied_up)
    if active and abs(dislocation) >= 0.06:
        # Push toward extreme in the CEX lead direction
        push = min(0.48, 0.30 + abs(dislocation))
        q = 0.5 + (1.0 if dislocation > 0 else -1.0) * push
        q = float(min(0.97, max(0.03, q)))
    elif not active or abs(dislocation) < cfg.min_edge * 0.5:
        q = 0.5 + 0.25 * (q - 0.5)

    market = MarketSnapshot(
        market_id=market_id,
        slug=slug,
        category="crypto",
        timeframe=timeframe,
        p=float(pm_implied_up),
        q=q,
        liquidity_usd=liquidity_usd,
        volume_24h=volume_24h,
        seconds_to_resolution=seconds_to_resolution,
        meta={
            "mispricing_active": active,
            "mispricing_dislocation": dislocation,
            "mispricing_conviction_raw": mp_conviction,
            "chainlink_vs_cex_bps": chainlink_vs_cex_bps,
            "entry_source": "enhanced_mispricing",
            "guard_active": guard.guard_active,
            "guard_reason": guard.reason,
        },
    )
    opp = evaluate_market(
        market,
        config=cfg,
        guard=guard,
        bankroll=bankroll if bankroll is not None else rm.state.bankroll,
    )
    # Attach guard snapshot for dashboard / verifier meta
    opp.meta["kappa"] = guard.kappa
    opp.meta["min_conviction_gate"] = guard.min_conviction
    opp.meta["drawdown_pct"] = guard.drawdown_pct
    opp.meta["rolling_wr"] = guard.rolling_wr
    return opp


def rank_and_select(
    markets: Sequence[MarketSnapshot],
    *,
    risk_manager: Optional[PortfolioRiskManager] = None,
    config: Optional[EnhancedMispriceConfig] = None,
) -> list[TradeOpportunity]:
    """Evaluate all markets, keep hard-filter passers, select inside risk budget."""
    cfg = config or load_enhanced_config()
    rm = risk_manager or PortfolioRiskManager(cfg)
    guard = rm.evaluate_guards()
    opps = [
        evaluate_market(m, config=cfg, guard=guard, bankroll=rm.state.bankroll)
        for m in markets
    ]
    selected = rm.select_within_budget(opps)
    logger.info(
        "enhanced_misprice: %d markets → %d pass filter → %d selected (guard=%s)",
        len(markets),
        sum(1 for o in opps if o.passes_hard_filter),
        len(selected),
        guard.reason,
    )
    return selected


def opportunity_to_signal_meta(opp: TradeOpportunity) -> dict[str, Any]:
    """Flatten opportunity fields into Hermes Signal.meta."""
    return {
        "enhanced_misprice": True,
        "enhanced_passes": opp.passes_hard_filter,
        "enhanced_conviction": round(opp.conviction, 5),
        "enhanced_conviction_score": round(opp.conviction_score, 6),
        "enhanced_edge": round(opp.edge, 5),
        "kelly_f_star": round(opp.kelly_f_star, 5),
        "kelly_f": round(opp.kelly_f, 5),
        "kelly_kappa": round(opp.kappa, 4),
        "kelly_size_usd": round(opp.size_usd, 2),
        "risk_unit": round(opp.risk_unit, 8),
        "liquidity_score": round(opp.liquidity_score, 4),
        "time_decay_factor": round(opp.time_decay_factor, 4),
        "enhanced_side": opp.side.value,
        "enhanced_reasons": opp.reasons[:6],
        **{k: v for k, v in (opp.meta or {}).items() if k not in ("enhanced_reasons",)},
    }
