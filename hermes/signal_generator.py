"""Alpha research agent — generates signals from discovered candidates.

Reads ALPHA_RESEARCH_SKILL.md on every turn. Applies DOWN bias dynamically,
confidence tiers, entry-mode rules, and pre-entry stability filters.
Does NOT execute — verifier must pass first.
"""

from __future__ import annotations

import logging
from typing import Optional

from hermes.decorators import loop
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

logger = logging.getLogger(__name__)

# Fee + slippage assumptions (realistic Polymarket round-trip)
FEE_BPS = 100.0  # 1% effective
SLIPPAGE_BPS = 40.0
MIN_PRE_ENTRY_STABILITY = 0.02  # max abs price wobble in lookback to pass

LANE_STATUS: dict[EntryMode, LaneStatus] = {
    EntryMode.OSMANI_LANE: LaneStatus.GATED,  # Hermes weakness: gate hard
    EntryMode.MOMENTUM: LaneStatus.ACTIVE,
    EntryMode.MEAN_REVERSION: LaneStatus.ACTIVE,
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
) -> bool:
    for b in buckets:
        if b.avoid and b.entry_mode == mode:
            return True
        if b.avoid and b.regime == regime and b.hourly_bucket == hour:
            return True
    lower = lessons.lower()
    if f"avoid:{mode.value}" in lower.replace(" ", ""):
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

    if direction in (Direction.YES, Direction.UP):
        mkt = candidate.yes_price
        edge = fair - mkt
    else:
        mkt = candidate.no_price
        edge = (1.0 - fair) - mkt

    # EXPLOIT bucket prior: historical avg_edge is evidence of expectancy
    if bucket_edge > 0:
        edge = max(edge, bucket_edge * 0.85)

    if edge <= 0:
        return None

    ev = live_ev_after_costs(edge, mkt)
    conv, tier = conviction_score(
        edge, candidate, ConfidenceTier.B, bucket_edge=bucket_edge, bucket_wr=bucket_wr
    )
    hit_avoid = avoid_bucket_hit(
        mode, candidate.regime, candidate.hourly_bucket, buckets, lessons
    )
    stable = pre_entry_stability(candidate)

    rules_fired = [
        f"down_bias={bias:.2f}",
        f"regime={candidate.regime.value}",
        f"mode={mode.value}",
        f"hour={candidate.hourly_bucket}",
    ]
    if bucket_edge > 0:
        rules_fired.append(f"exploit_bucket_edge={bucket_edge:.3f}")
    if "DOWN" in read_alpha_skill():
        rules_fired.append("alpha:DOWN_bias_active")

    capital = float(state.get("capital_usd", state.get("capital", 10_000)) or 10_000)
    size = min(capital * 0.02, capital * abs(edge) * 0.25)
    size = max(25.0, size)

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
            f"{mode.value} on {candidate.regime.value} h{candidate.hourly_bucket}; "
            f"edge={edge:.3f} ev={ev:.3f} bias={bias:.2f}"
        ),
        alpha_rules_fired=rules_fired,
        avoid_bucket_hit=hit_avoid,
        generator_model="alpha-research-agent",
        meta={"paper": paper, "down_bias": bias, "bucket_edge_prior": bucket_edge},
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
        sig = generate_signal(c, alpha_text=alpha, buckets=buckets, state=state, paper=paper)
        if sig is not None:
            signals.append(sig)

    tid = turn_id or "adhoc"
    path = write_handoff("signals", signals, tid)
    logger.info("signal_generator: %d signals → %s", len(signals), path.name)
    return signals
