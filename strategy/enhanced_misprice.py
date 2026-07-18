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
from strategy.kelly import kelly_size, net_edge_after_costs
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
    live_real_q: bool = False,
) -> TradeOpportunity:
    """Run enhanced filters + Kelly sizing on one market snapshot.

    Parameters
    ----------
    market.p : Polymarket implied P(YES/UP)
    market.q : model fair P(YES/UP) — from CEX mispricing or other pluggable model
    live_real_q : when True (Hermes live paper), mid CEX q uses Polymarket p
        stretch gates instead of requiring q≥0.85 (which never fires live).
    """
    # FIXED: Removed artificial q pushing. Using real cex_implied probability to stop hallucination.
    cfg = config or load_enhanced_config()
    kappa = guard.kappa if guard else cfg.kappa_base
    min_conv = guard.min_conviction if guard else cfg.min_conviction
    br = float(bankroll if bankroll is not None else cfg.bankroll)

    side = _side_from_q_p(market.q, market.p)
    # Price paid for the chosen contract
    p_side = market.p if side in (Side.YES, Side.UP) else (1.0 - market.p)
    q_side = market.q if side in (Side.YES, Side.UP) else (1.0 - market.q)
    edge = abs(market.q - market.p)  # gross, for logging/back-compat
    # Task 5: net expected slippage + fees out of the edge BEFORE the
    # min_edge gate and BEFORE Kelly. Costs are never free.
    slip_bps = 0.5 * (float(cfg.slippage_bps_min) + float(cfg.slippage_bps_max))
    net_edge = net_edge_after_costs(
        p_side, q_side, slippage_bps=slip_bps, fee_bps=float(cfg.settlement_fee_bps)
    )
    # Cost-adjusted model prob so Kelly sizes on the NET edge: for the chosen
    # side, q_eff_side = p_side + net_edge; map back to YES/UP terms.
    q_eff = (
        market.p + net_edge if side in (Side.YES, Side.UP) else market.p - net_edge
    )
    q_eff = float(min(1.0, max(0.0, q_eff)))

    n_eff = cfg.n_eff.for_category(market.category)
    bayes = bayesian_conviction(market.q, market.p, n_eff, side=side.value)

    # Clear entry consideration log: real q, live Polymarket p, edge, conviction
    logger.info(
        "entry_consider q=%.4f p=%.4f edge=%.4f conviction=%.4f side=%s slug=%s",
        market.q,
        market.p,
        edge,
        bayes.conviction,
        side.value,
        market.slug or market.market_id,
    )

    ok, fail_reasons = passes_hard_entry_filter(
        market.q,
        market.p,
        bayes.conviction,
        min_edge=cfg.min_edge,
        min_conviction=min_conv,
        extreme_q_high=cfg.extreme_q_high,
        extreme_q_low=cfg.extreme_q_low,
        extreme_anchor=getattr(cfg, "extreme_anchor", "q"),
        live_real_q=live_real_q,
        extreme_p_high=getattr(cfg, "extreme_p_high", None),
        extreme_p_low=getattr(cfg, "extreme_p_low", None),
        net_edge=net_edge,
    )

    liq = liquidity_score(market.liquidity_usd, market.volume_24h)
    tdf = time_decay_factor(market.seconds_to_resolution)
    score = conviction_score(market.q, market.p, bayes.conviction, liq, tdf)

    reasons: list[str] = []
    if guard and guard.guard_active:
        reasons.append(f"guard_active:{guard.reason}")

    kelly = kelly_size(
        q=q_eff,  # cost-adjusted: Kelly sizes on the net edge, not gross
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
        logger.info(
            "entry_skip q=%.4f p=%.4f edge=%.4f conviction=%.4f reasons=%s slug=%s",
            market.q,
            market.p,
            edge,
            bayes.conviction,
            fail_reasons[:4],
            market.slug or market.market_id,
        )
    else:
        reasons.append(
            f"PASS edge={edge:.3f} conv={bayes.conviction:.3f} "
            f"kelly_f={kelly.f:.4f} size=${size:.2f}"
        )
        logger.info(
            "entry_pass q=%.4f p=%.4f edge=%.4f conviction=%.4f size=$%.2f slug=%s",
            market.q,
            market.p,
            edge,
            bayes.conviction,
            size,
            market.slug or market.market_id,
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
            "gross_edge": float(edge),
            "net_edge": float(net_edge),
            "cost_frac": float(edge - net_edge),
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
    advanced_features: Optional[dict[str, Any]] = None,
) -> TradeOpportunity:
    """Wrap an existing Hermes ``MispricingSignal`` into enhanced filters.

    q := CEX-implied P(UP) (lead signal), lightly smoothed — never forced to 0.97/0.03
    p := Polymarket YES price (live CLOB mid)

    When ``advanced_features`` carries ensemble diagnostics (hurst/obi/kalman),
    they are attached to the snapshot; q itself already comes from the ensemble
    via ``cex_implied_up`` when history is available. Hard filters stay intact.
    """
    # FIXED: Removed artificial q pushing. Using real cex_implied probability to stop hallucination.
    cfg = config or load_enhanced_config()
    rm = risk_manager or PortfolioRiskManager(cfg)
    guard = rm.evaluate_guards()

    # Prefer advanced ensemble q when present in features (already in cex_implied_up).
    adv = dict(advanced_features or {})
    q_raw = float(cex_implied_up)
    if adv.get("advanced_q") is not None:
        try:
            q_raw = float(adv["advanced_q"])
        except (TypeError, ValueError):
            pass

    # Use live CEX-implied P(UP) directly as model q.
    # Light shrink toward 0.5 for calibration only — NEVER force 0.97/0.03 on dislocation.
    if not active:
        # Inactive setups: shrink harder so they rarely clear extreme_q gates
        q = 0.5 + 0.25 * (q_raw - 0.5)
    else:
        q = 0.5 + 0.90 * (q_raw - 0.5)  # light smoothing of real cex_implied_up
    q = float(min(0.95, max(0.05, q)))

    # Log strong disagreement between advanced q and toy momentum (selectivity)
    if adv.get("momentum") is not None and adv.get("advanced_used_fallback") == 0.0:
        try:
            from strategy.advanced_signals import momentum_to_q

            toy = momentum_to_q(float(adv.get("momentum", 0.0)), timeframe)
            if abs(q_raw - toy) > 0.12:
                logger.info(
                    "advanced/momentum disagree slug=%s adv_q=%.3f toy_q=%.3f hurst=%s",
                    slug,
                    q_raw,
                    toy,
                    adv.get("hurst"),
                )
        except Exception:  # noqa: BLE001
            pass

    q_source = "cex_implied_up_smoothed"
    if adv.get("advanced_used_fallback") == 0.0:
        q_source = "advanced_ensemble_smoothed"
    elif adv.get("advanced_q") is not None:
        q_source = "advanced_fallback_smoothed"

    pm_p = float(pm_implied_up)
    logger.info(
        "enhanced_q slug=%s cex_implied_up=%.4f q=%.4f p=%.4f edge=%.4f "
        "dislocation=%+.4f active=%s src=%s (real cex_implied, no hallucination push)",
        slug,
        q_raw,
        q,
        pm_p,
        abs(q - pm_p),
        dislocation,
        active,
        q_source,
    )

    slopes = {
        k: float(v)
        for k, v in adv.items()
        if str(k).startswith("slope_") and isinstance(v, (int, float))
    } or None

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
        multi_level_obi=float(adv["obi"]) if adv.get("obi") is not None else None,
        ir=float(adv["ir"]) if adv.get("ir") is not None else None,
        vamp=float(adv["vamp"]) if adv.get("vamp") is not None else None,
        hurst=float(adv["hurst"]) if adv.get("hurst") is not None else None,
        ou_theta=float(adv["ou_theta"]) if adv.get("ou_theta") is not None else None,
        kalman_q=float(adv["kalman_q"]) if adv.get("kalman_q") is not None else None,
        garch_vol=(
            float(adv["garch_sigma_ann"])
            if adv.get("garch_sigma_ann") is not None
            else None
        ),
        multi_tf_slopes=slopes,
        meta={
            "mispricing_active": active,
            "mispricing_dislocation": dislocation,
            "mispricing_conviction_raw": mp_conviction,
            "chainlink_vs_cex_bps": chainlink_vs_cex_bps,
            "entry_source": "enhanced_mispricing",
            "cex_implied_up_raw": q_raw,
            "model_q_source": q_source,
            "guard_active": guard.guard_active,
            "guard_reason": guard.reason,
            "advanced_regime": adv.get("advanced_regime_mom"),
            "hurst": adv.get("hurst"),
            "obi": adv.get("obi"),
            "kalman_q": adv.get("kalman_q"),
        },
    )
    opp = evaluate_market(
        market,
        config=cfg,
        guard=guard,
        bankroll=bankroll if bankroll is not None else rm.state.bankroll,
        # Live Hermes path: real cex_implied_up never hits q≥0.85 — use PM p stretch.
        live_real_q=True,
    )
    # Attach guard snapshot for dashboard / verifier meta
    opp.meta["kappa"] = guard.kappa
    opp.meta["min_conviction_gate"] = guard.min_conviction
    opp.meta["drawdown_pct"] = guard.drawdown_pct
    opp.meta["rolling_wr"] = guard.rolling_wr
    opp.meta["live_real_q"] = True
    return opp


def filter_markets_by_scope(
    markets: Sequence[MarketSnapshot],
    *,
    market_filter: Optional[str] = None,
) -> list[MarketSnapshot]:
    """Keep only markets matching MARKET_FILTER / market_filter param."""
    from hermes.market_scope import is_allowed_slug, matches_market_filter, scope_enabled

    if not scope_enabled() and not market_filter:
        return list(markets)
    out: list[MarketSnapshot] = []
    for m in markets:
        if m.slug and is_allowed_slug(m.slug, market_filter=market_filter):
            out.append(m)
            continue
        if matches_market_filter(
            slug=m.slug or "",
            series=str((m.meta or {}).get("scoped_series") or ""),
            asset=str((m.meta or {}).get("asset") or m.category or ""),
            timeframe=m.timeframe or "",
            market_filter=market_filter,
        ):
            out.append(m)
    return out


def rank_and_select(
    markets: Sequence[MarketSnapshot],
    *,
    risk_manager: Optional[PortfolioRiskManager] = None,
    config: Optional[EnhancedMispriceConfig] = None,
    market_filter: Optional[str] = None,
    max_trades: Optional[int] = None,
) -> list[TradeOpportunity]:
    """Evaluate markets, keep hard-filter passers, select inside risk budget.

    Parameters
    ----------
    market_filter:
        Optional override (btc5 / btc15 / eth5 / sol5 / rotator). Defaults to
        env ``MARKET_FILTER``.
    max_trades:
        Cap selected tickets. Rotator instances default to 1 (highest conviction).
    """
    from hermes.market_scope import is_rotator, market_filter as active_mf

    cfg = config or load_enhanced_config()
    rm = risk_manager or PortfolioRiskManager(cfg)
    guard = rm.evaluate_guards()
    scoped = filter_markets_by_scope(markets, market_filter=market_filter)
    opps = [
        evaluate_market(m, config=cfg, guard=guard, bankroll=rm.state.bankroll)
        for m in scoped
    ]
    # Highest conviction first before risk budget greedy
    opps_sorted = sorted(
        [o for o in opps if o.passes_hard_filter],
        key=lambda o: o.conviction_score,
        reverse=True,
    )
    selected = rm.select_within_budget(opps_sorted)
    mf = market_filter or active_mf()
    cap = max_trades
    if cap is None and (is_rotator() or mf == "rotator"):
        cap = 1
    if cap is not None and cap >= 0:
        selected = selected[:cap]
    logger.info(
        "enhanced_misprice: %d markets → %d scoped → %d pass → %d selected "
        "(filter=%s guard=%s)",
        len(markets),
        len(scoped),
        sum(1 for o in opps if o.passes_hard_filter),
        len(selected),
        mf,
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
