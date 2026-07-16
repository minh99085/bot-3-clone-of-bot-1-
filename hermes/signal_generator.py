"""Alpha research agent — generates signals from discovered candidates.

Reads ALPHA_RESEARCH_SKILL.md on every turn. Applies DOWN bias dynamically,
confidence tiers, entry-mode rules, and pre-entry stability filters.
Does NOT execute — verifier must pass first.
"""

from __future__ import annotations

import logging
from typing import Optional

from hermes.decorators import loop
from hermes.market_scope import (
    is_extreme_entry_price,
    is_extreme_market_price,
    is_window_tradeable,
    resolve_asset,
)
from hermes.models import (
    ConfidenceTier,
    Direction,
    EdgeBucket,
    EntryMode,
    LaneStatus,
    MarketCandidate,
    Regime,
    Signal,
)
from hermes.discovery import load_edge_buckets_from_alpha
from hermes.state_io import (
    ensure_dirs,
    parse_state_fields,
    read_alpha_skill,
    read_lessons_md,
    read_state_md,
    write_handoff,
)
from hermes.substrategy import annotate_signal, infer_market_series

logger = logging.getLogger(__name__)

# Fee + slippage assumptions (realistic Polymarket round-trip)
FEE_BPS = 100.0  # 1% effective
SLIPPAGE_BPS = 40.0
MIN_PRE_ENTRY_STABILITY = 0.02  # max abs price wobble in lookback to pass

LANE_STATUS: dict[EntryMode, LaneStatus] = {
    EntryMode.OSMANI_LANE: LaneStatus.GATED,  # Hermes weakness: gate hard
    EntryMode.MOMENTUM: LaneStatus.ACTIVE,
    EntryMode.MEAN_REVERSION: LaneStatus.ACTIVE,
    EntryMode.MISPRICING: LaneStatus.ACTIVE,
    EntryMode.NEWS_SHOCK: LaneStatus.PAPER_ONLY,
    EntryMode.LIQUIDITY_SWEEP: LaneStatus.ACTIVE,
    EntryMode.GROK_SIGNAL: LaneStatus.PAPER_ONLY,
    EntryMode.TV_SIGNAL: LaneStatus.PAPER_ONLY,
}


def dynamic_down_bias(regime: Regime, state: dict) -> float:
    """Explicit, dynamic DOWN bias in [-1, +1]. Positive => prefer DOWN/NO.

    Hermes post-mortem: DOWN bias must be explicit and adaptive, not hardcoded
    as a constant that ignores regime.
    """
    base = float(state.get("down_bias", 0.35) or 0.35)
    if regime == Regime.TRENDING_DOWN:
        return min(1.0, base + 0.25)
    if regime == Regime.TRENDING_UP:
        return max(-0.2, base - 0.40)
    if regime == Regime.HIGH_VOL:
        return min(1.0, base + 0.10)  # slightly more defensive
    if regime == Regime.MEAN_REVERT:
        return base
    return base


def choose_direction(
    candidate: MarketCandidate,
    fair_yes: float,
    down_bias: float,
) -> Direction:
    """Pick YES/NO (mapped to UP/DOWN semantics) with DOWN bias applied."""
    edge_yes = fair_yes - candidate.yes_price
    edge_no = (1.0 - fair_yes) - candidate.no_price
    # Tilt edges by bias: positive down_bias boosts NO/DOWN
    tilted_yes = edge_yes - 0.5 * down_bias * abs(edge_yes)
    tilted_no = edge_no + 0.5 * down_bias * abs(edge_no)
    if tilted_no >= tilted_yes:
        return Direction.NO
    return Direction.YES


def estimate_fair_value(
    candidate: MarketCandidate,
    alpha_text: str,
    *,
    direction_hint: Optional[Direction] = None,
    bucket_edge: float = 0.0,
) -> float:
    """Toy fair-value model — replace with calibrated research model.

    Uses mid-price shrinkage toward 0.5 plus regime tilt. When an EXPLOIT
    bucket provides historical avg_edge, apply it as a prior so paper demos
    and cold-start research can clear verifier gates honestly.
    """
    mid = candidate.yes_price
    shrink = 0.5 + 0.7 * (mid - 0.5)
    if candidate.regime == Regime.TRENDING_DOWN:
        shrink -= 0.03
    elif candidate.regime == Regime.TRENDING_UP:
        shrink += 0.03
    # Apply historical bucket edge as a fair-value prior (signed toward value)
    if bucket_edge > 0:
        # Assume EXPLOIT DOWN-biased buckets: push fair YES down (favor NO)
        shrink -= min(0.12, bucket_edge)
    lessons = read_lessons_md().lower()
    if "overestimate yes" in lessons or "yes overpriced" in lessons:
        shrink -= 0.02
    _ = alpha_text, direction_hint
    return float(min(0.95, max(0.05, shrink)))


def pick_entry_mode(candidate: MarketCandidate, buckets: list[EdgeBucket]) -> EntryMode:
    for b in buckets:
        if b.exploit and b.regime == candidate.regime and not b.avoid:
            if LANE_STATUS.get(b.entry_mode, LaneStatus.ACTIVE) != LaneStatus.KILLED:
                if LANE_STATUS.get(b.entry_mode) != LaneStatus.GATED:
                    return b.entry_mode
    if candidate.regime == Regime.MEAN_REVERT:
        return EntryMode.MEAN_REVERSION
    if candidate.regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
        return EntryMode.MOMENTUM
    return EntryMode.MEAN_REVERSION


def conviction_score(
    edge: float,
    candidate: MarketCandidate,
    tier_hint: ConfidenceTier,
    *,
    bucket_edge: float = 0.0,
    bucket_wr: float = 0.0,
) -> tuple[float, ConfidenceTier]:
    raw = min(1.0, abs(edge) * 4.0)
    if candidate.liquidity > 20_000 and candidate.spread_bps < 100:
        raw = min(1.0, raw + 0.1)
    if candidate.regime == Regime.UNKNOWN:
        raw *= 0.7
    # Historical EXPLOIT evidence raises conviction (verifier still checks bucket WR)
    if bucket_edge > 0 and bucket_wr >= 0.65:
        raw = min(1.0, raw + 0.35 + 0.2 * (bucket_wr - 0.65))
    if raw >= 0.75:
        tier = ConfidenceTier.A
    elif raw >= 0.55:
        tier = ConfidenceTier.B
    elif raw >= 0.35:
        tier = ConfidenceTier.C
    else:
        tier = ConfidenceTier.D
    return raw, tier


def live_ev_after_costs(edge: float, price: float) -> float:
    """EV after fees + slippage. Verifier requires > 0.06–0.08."""
    cost = (FEE_BPS + SLIPPAGE_BPS) / 10_000.0
    return edge - cost - 0.01 * max(0.0, price - 0.5)


def pre_entry_stability(candidate: MarketCandidate, lookback_wobble: float = 0.01) -> bool:
    """Tighter pre-entry stability filter (Hermes execution-drag fix)."""
    wobble = candidate.spread_bps / 10_000.0 * 2.0
    return wobble <= max(MIN_PRE_ENTRY_STABILITY, lookback_wobble)


def entry_vwap_target(price: float, direction: Direction) -> float:
    """Tighter entry VWAP — don't chase; sit inside the spread."""
    cushion = 0.005
    if direction in (Direction.YES, Direction.UP):
        return max(0.01, price - cushion)
    return min(0.99, price + cushion)


def avoid_bucket_hit(
    mode: EntryMode,
    regime: Regime,
    hour: int,
    buckets: list[EdgeBucket],
    lessons: str,
    *,
    series: str = "",
    substrategy_id: str = "",
) -> bool:
    for b in buckets:
        if b.avoid and b.entry_mode == mode:
            return True
        if b.avoid and b.regime == regime and b.hourly_bucket == hour:
            return True
    lower = lessons.lower()
    compact = lower.replace(" ", "")
    mode_key = f"avoid:{mode.value}"
    if mode_key in compact:
        # Series/sleeve-scoped AVOID only — do not block eth5 on btc5 lesson.
        if series and f"`{series}`" in lower:
            return True
        if substrategy_id and substrategy_id in lessons:
            return True
        if f"hour={hour}" in lower and series and f"`{series}`" in lower:
            return True
    if LANE_STATUS.get(mode) in (LaneStatus.GATED, LaneStatus.KILLED):
        return True
    return False


def generate_signal(
    candidate: MarketCandidate,
    *,
    alpha_text: str,
    buckets: list[EdgeBucket],
    state: dict,
    paper: bool = True,
) -> Optional[Signal]:
    from hermes.bandit import get_bandit
    from hermes.mispricing import detect_mispricing

    lessons = read_lessons_md()
    mode = pick_entry_mode(candidate, buckets)
    if LANE_STATUS.get(mode) == LaneStatus.KILLED:
        logger.info("skip %s: lane killed", candidate.market_id)
        return None
    if LANE_STATUS.get(mode) == LaneStatus.GATED:
        logger.info("skip %s: lane gated (%s)", candidate.market_id, mode.value)
        return None
    if LANE_STATUS.get(mode) == LaneStatus.PAPER_ONLY and not paper:
        return None

    raw = candidate.raw or {}
    tf = candidate.timeframe or raw.get("timeframe") or "1h"
    asset_u = resolve_asset(candidate.slug or "", meta=raw)

    from hermes.market_scope import is_extreme_market_price, is_window_tradeable

    if candidate.slug and not is_window_tradeable(candidate.slug):
        logger.info("signal skip expired/late window slug=%s", candidate.slug)
        return None
    if is_extreme_market_price(float(candidate.yes_price)):
        logger.info(
            "signal skip extreme yes_price=%.4f slug=%s",
            candidate.yes_price,
            candidate.slug,
        )
        return None

    cl_price = None
    try:
        if raw.get("oracle_price") is not None:
            cl_price = float(raw["oracle_price"])
    except (TypeError, ValueError):
        cl_price = None

    # Option D: CEX↔PM mispricing (primary alpha for fast BTC windows)
    mp = detect_mispricing(candidate, chainlink_price=cl_price)

    # Pull EXPLOIT bucket prior if available
    bucket_edge = 0.0
    bucket_wr = 0.0
    for b in buckets:
        if (
            b.exploit
            and not b.avoid
            and b.entry_mode == mode
            and b.regime == candidate.regime
        ):
            if b.avg_edge >= bucket_edge:
                bucket_edge = b.avg_edge
                bucket_wr = b.win_rate

    fair = estimate_fair_value(candidate, alpha_text, bucket_edge=bucket_edge)
    bias = dynamic_down_bias(candidate.regime, state)
    direction = choose_direction(candidate, fair, bias)

    # Mispricing overrides direction when active and strong enough
    if mp.active and mp.direction is not None and mp.conviction >= 0.45:
        direction = mp.direction
        mode = EntryMode.MISPRICING
        # Fair ≈ CEX-implied for the chosen side
        if direction in (Direction.UP, Direction.YES):
            fair = mp.cex_implied_up
        else:
            fair = 1.0 - mp.cex_implied_up

    if is_extreme_entry_price(float(candidate.yes_price), direction.value):
        logger.info(
            "signal skip extreme entry yes=%.4f dir=%s slug=%s",
            candidate.yes_price,
            direction.value,
            candidate.slug,
        )
        return None

    if direction in (Direction.YES, Direction.UP):
        mkt = candidate.yes_price
        edge = fair - mkt
    else:
        mkt = candidate.no_price
        edge = (1.0 - fair) - mkt

    # Boost edge from dislocation magnitude (must clear soft EV after fees)
    if mp.active:
        edge = max(edge, abs(mp.dislocation) * 1.15, 0.055 * mp.conviction + 0.03)

    if bucket_edge > 0 and not mp.active:
        edge = max(edge, bucket_edge * 0.85)

    # Allow mispricing-driven signals even if classic edge was flat
    if edge <= 0 and not (mp.active and mp.conviction >= 0.5):
        return None
    if edge <= 0 and mp.active:
        edge = max(0.02, abs(mp.dislocation) * 0.7)

    ev = live_ev_after_costs(edge, mkt)
    conv, tier = conviction_score(
        edge, candidate, ConfidenceTier.B, bucket_edge=bucket_edge, bucket_wr=bucket_wr
    )
    if mp.active:
        conv = min(1.0, max(conv, 0.45 + 0.5 * mp.conviction))
        if conv >= 0.75:
            tier = ConfidenceTier.A
        elif conv >= 0.55:
            tier = ConfidenceTier.B
        else:
            tier = ConfidenceTier.C

    hit_avoid = avoid_bucket_hit(
        mode,
        candidate.regime,
        candidate.hourly_bucket,
        buckets,
        lessons,
        series=infer_market_series(
            candidate.market_id, candidate.slug, candidate.question
        ),
    )
    stable = pre_entry_stability(candidate)

    # Bandit: explore / exploit / skip recommendation (pretrade enforces skip)
    bandit = get_bandit()
    decision = bandit.decide(mp, candidate.hourly_bucket)
    bandit.record_pull(decision)

    # MCHB hierarchical gate (Thompson family + LinUCB leaf) — may force skip
    mchb_meta: dict = {}
    try:
        from autonomy.orchestrator import mchb_gate
        from hermes.market_scope import window_remaining_seconds as _wrem

        _rem = _wrem(candidate.slug) if candidate.slug else None
        mchb_arm, mchb_meta = mchb_gate(
            {
                "timeframe": str(tf),
                "seconds_to_resolution": float(_rem if _rem is not None else (300 if tf == "5m" else 900)),
                "liquidity_usd": float(getattr(candidate, "liquidity", None) or 5_000),
                "momentum": float(mp.cex_momentum),
                "dislocation": float(mp.dislocation),
                "hurst": (mp.features or {}).get("hurst"),
                "garch_vol": (mp.features or {}).get("garch_sigma_ann"),
            }
        )
        if mchb_arm == "skip" and decision.arm != "skip":
            # Hierarchical skip only when uncertainty low and family weak
            if mchb_meta.get("mchb_forced"):
                logger.info(
                    "signal skip mchb family gate slug=%s unc=%.3f",
                    candidate.slug,
                    float(mchb_meta.get("mchb_uncertainty") or 0),
                )
                return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("mchb gate skipped: %s", exc)

    capital = float(
        state.get("capital_usd", state.get("capital", state.get("starting_bankroll_usd", 2000)))
        or 2000
    )
    size = min(capital * 0.02, capital * abs(edge) * 0.25)
    size = max(10.0, size)

    # Enhanced misprice layer (Kelly + Beta conviction) — wraps Option D
    enhanced_meta: dict = {}
    try:
        from strategy.enhanced_misprice import (
            enhance_from_hermes_mispricing,
            opportunity_to_signal_meta,
        )
        from hermes.market_scope import window_remaining_seconds

        rem = window_remaining_seconds(candidate.slug) if candidate.slug else None
        if rem is not None:
            sec_res = max(30.0, float(rem))
        else:
            sec_res = 300.0 if tf == "5m" else 900.0

        opp = enhance_from_hermes_mispricing(
            market_id=candidate.market_id,
            slug=candidate.slug,
            pm_implied_up=float(candidate.yes_price),
            cex_implied_up=float(mp.cex_implied_up),
            dislocation=float(mp.dislocation),
            mp_conviction=float(mp.conviction),
            timeframe=str(tf),
            liquidity_usd=float(getattr(candidate, "liquidity", None) or 5_000),
            volume_24h=float(getattr(candidate, "volume_24h", None) or 10_000),
            seconds_to_resolution=sec_res,
            chainlink_vs_cex_bps=float(mp.chainlink_vs_cex_bps),
            active=bool(mp.active),
            bankroll=capital,
            advanced_features=dict(mp.features or {}),
        )
        enhanced_meta = opportunity_to_signal_meta(opp)
        if not opp.passes_hard_filter:
            logger.info(
                "signal skip enhanced filter fail slug=%s reasons=%s",
                candidate.slug,
                (opp.reasons or [])[:3],
            )
            return None
        if opp.passes_hard_filter:
            # Prefer enhanced direction + Kelly-suggested size
            if opp.side.value in ("YES", "UP"):
                direction = Direction.UP if tf in ("5m", "15m") else Direction.YES
                fair = opp.q
                mkt = candidate.yes_price
            else:
                direction = Direction.DOWN if tf in ("5m", "15m") else Direction.NO
                fair = 1.0 - opp.q
                mkt = candidate.no_price
            mode = EntryMode.MISPRICING
            edge = max(edge, opp.edge)
            ev = live_ev_after_costs(edge, mkt)
            conv = min(1.0, max(conv, opp.conviction))
            tier = ConfidenceTier.A if conv >= 0.75 else ConfidenceTier.B
            size = max(10.0, min(float(opp.size_usd), capital * 0.10))
            # RGMC soft size multiplier (tighten-only)
            try:
                from autonomy.orchestrator import apply_soft_sizing

                size, _ = apply_soft_sizing(size, float(opp.kappa))
                size = max(10.0, min(size, capital * 0.10))
            except Exception:  # noqa: BLE001
                pass
            if mchb_meta.get("mchb_arm") == "explore":
                size = max(10.0, size * 0.5)
    except Exception as exc:  # noqa: BLE001 — never break the loop
        logger.warning("enhanced_misprice failed: %s", exc)
        return None

    if not enhanced_meta.get("enhanced_passes"):
        return None

    if mchb_meta:
        enhanced_meta.update(mchb_meta)

    rules_fired = [
        f"down_bias={bias:.2f}",
        f"regime={candidate.regime.value}",
        f"mode={mode.value}",
        f"hour={candidate.hourly_bucket}",
        f"bandit={decision.arm}",
    ]
    if mchb_meta.get("mchb_arm"):
        rules_fired.append(f"mchb={mchb_meta['mchb_arm']}")
    if mp.active:
        rules_fired.append(f"mispricing={mp.dislocation:+.3f}")
    if enhanced_meta.get("enhanced_passes"):
        rules_fired.append(
            f"enhanced_kelly={enhanced_meta.get('kelly_f')} "
            f"conv={enhanced_meta.get('enhanced_conviction')}"
        )
    if bucket_edge > 0:
        rules_fired.append(f"exploit_bucket_edge={bucket_edge:.3f}")

    oracle_align = float(raw.get("oracle_alignment") or 0.5)
    if tf in ("5m", "15m") and raw.get("asset"):
        conv = min(1.0, conv * (0.75 + 0.25 * oracle_align))
        if mp.active:
            conv = min(1.0, max(conv, mp.conviction * 0.9))
        if enhanced_meta.get("enhanced_passes"):
            conv = min(1.0, max(conv, float(enhanced_meta.get("enhanced_conviction") or conv)))
        if conv >= 0.75:
            tier = ConfidenceTier.A
        elif conv >= 0.55:
            tier = ConfidenceTier.B
        elif conv >= 0.35:
            tier = ConfidenceTier.C
        else:
            tier = ConfidenceTier.D

    # Soft-skip: still emit signal so lessons/dashboard see bandit SKIP,
    # but mark for pretrade to size 0
    meta = {
        "paper": paper,
        "down_bias": bias,
        "bucket_edge_prior": bucket_edge,
        "asset": asset_u,
        "oracle_return_proxy": raw.get("oracle_return_proxy"),
        **mp.as_meta(),
        **decision.as_meta(),
        **enhanced_meta,
        "cex_mid": mp.cex_mid,
        "cex_asset": asset_u,
        "yes_price": float(candidate.yes_price),
        "cex_ret_60s": mp.features.get("ret_60s", 0.0),
    }

    return Signal(
        market_id=candidate.market_id,
        slug=candidate.slug,
        question=candidate.question,
        direction=direction,
        entry_mode=mode,
        confidence_tier=tier,
        conviction=conv,
        fair_value=fair if direction in (Direction.YES, Direction.UP) else 1.0 - fair,
        market_price=mkt,
        expected_edge=edge,
        live_ev=ev,
        regime=candidate.regime,
        hourly_bucket=candidate.hourly_bucket,
        size_usd_suggested=round(size, 2),
        entry_vwap_target=entry_vwap_target(mkt, direction),
        pre_entry_stability_ok=stable,
        rationale=(
            f"{mode.value} {tf} h{candidate.hourly_bucket}; "
            f"edge={edge:.3f} ev={ev:.3f} bandit={decision.arm}; "
            f"enhanced={enhanced_meta.get('enhanced_passes')}; "
            f"{mp.reason or 'no_mispricing'}"
        ),
        alpha_rules_fired=rules_fired + [f"tf={tf}", f"oracle_align={oracle_align:.2f}"],
        avoid_bucket_hit=hit_avoid,
        market_series=infer_market_series(
            candidate.market_id, candidate.slug, candidate.question
        ),
        timeframe=tf,
        oracle_price=float(raw["oracle_price"]) if raw.get("oracle_price") is not None else None,
        oracle_source=str(raw.get("oracle_source") or ""),
        oracle_alignment=oracle_align,
        oracle_stale=bool(raw.get("oracle_stale") or False),
        clob_token_id=(
            str(raw["yes_token_id"])
            if direction in (Direction.YES, Direction.UP) and raw.get("yes_token_id")
            else (str(raw["no_token_id"]) if raw.get("no_token_id") else None)
        ),
        generator_model="alpha-research-agent+mispricing-bandit+enhanced-kelly",
        meta=meta,
    )


@loop(interval="5m", name="signal_generator")
def signal_generator_tick(
    candidates: Optional[list[MarketCandidate]] = None,
    turn_id: Optional[str] = None,
    paper: bool = True,
) -> list[Signal]:
    ensure_dirs()
    alpha = read_alpha_skill()
    state = parse_state_fields(read_state_md())
    buckets = load_edge_buckets_from_alpha()

    if candidates is None:
        # Re-run discovery if no handoff provided
        from hermes.discovery import discovery_tick

        candidates = discovery_tick(turn_id=turn_id)

    signals: list[Signal] = []
    for c in candidates:
        from hermes.market_scope import is_allowed_slug, scope_enabled

        if scope_enabled() and not is_allowed_slug(c.slug):
            logger.info("signal skip out-of-scope slug=%s", c.slug)
            continue
        sig = generate_signal(c, alpha_text=alpha, buckets=buckets, state=state, paper=paper)
        if sig is not None:
            signals.append(annotate_signal(sig))

    # Rotator: only enhanced-pass signals; keep single highest conviction
    from hermes.market_scope import is_rotator

    if is_rotator():
        signals = [s for s in signals if (s.meta or {}).get("enhanced_passes")]
        if not signals:
            logger.info("rotator: no enhanced_pass signals this turn")
        elif len(signals) > 1:
            def _conv(s: Signal) -> float:
                meta = s.meta or {}
                return float(
                    meta.get("enhanced_conviction_score")
                    or meta.get("enhanced_conviction")
                    or s.confidence
                    or 0.0
                )

            signals = [max(signals, key=_conv)]
            logger.info(
                "rotator: kept top conviction signal slug=%s conv=%.4f",
                signals[0].slug,
                _conv(signals[0]),
            )

    tid = turn_id or "adhoc"
    path = write_handoff("signals", signals, tid)
    logger.info("signal_generator: %d signals → %s", len(signals), path.name)
    return signals
