"""Verifier — maker-checker. The thing that can say NO.

This is the single most important module for hitting consistent 80%+ WR.
The generator (signal_generator) is structurally barred from grading its own
homework. This checker uses different instructions, a stronger model hint,
and objective numeric gates. Default stance: assume the signal is broken
until every check passes.

Strict gates (all must pass for PASS):
  1. Historical edge in exact bucket/mode/regime > threshold
  2. Live EV after slippage + fees > MIN_LIVE_EV (0.06–0.08)
  3. Multi-timeframe regime filter + conviction check
  4. Not in any AVOID bucket from LESSONS.md / alpha skill
  5. Position sizing respects drawdown + correlation rules
  6. Pre-entry stability + entry VWAP present
  7. Confidence tier in {A, B} only
  8. Lane not GATED/KILLED
  9. Allocation approved — size/weight does not degrade portfolio metrics
 10. Sub-strategy not CUT (model broken ≠ currently losing)
"""

from __future__ import annotations

import logging
from typing import Optional

from hermes.decorators import checker, loop
from hermes.discovery import load_edge_buckets_from_alpha
from hermes.models import (
    AllocationProposal,
    CheckResult,
    ConfidenceTier,
    EdgeBucket,
    EntryMode,
    LaneStatus,
    Signal,
    SubStrategyAction,
    VerificationReport,
    VerifierDecision,
)
from hermes.signal_generator import LANE_STATUS
from hermes.state_io import (
    ensure_dirs,
    parse_state_fields,
    push_inbox,
    read_lessons_md,
    read_state_md,
    write_handoff,
)
from hermes.substrategy import annotate_signal

logger = logging.getLogger(__name__)

# ── Objective thresholds (tuned for 80%+ WR ambition) ──────────────────────
MIN_BUCKET_WR = 0.65
MIN_BUCKET_N = 20
MIN_BUCKET_EDGE = 0.05
MIN_LIVE_EV = 0.06  # floor; prefer 0.08 when capital at risk
PREFERRED_LIVE_EV = 0.08
MIN_CONVICTION = 0.55
MIN_PROFIT_FACTOR = 1.4
MAX_BUCKET_DD = 0.12
MAX_PORTFOLIO_DD = 0.08
MAX_SINGLE_POSITION_PCT = 0.03
MAX_CORRELATED_EXPOSURE_PCT = 0.08
MAX_HHI = 0.45  # concentration reject if portfolio would become too peaked
MIN_DIV_RATIO = 1.05  # reject if allocation destroys diversification
ALLOWED_TIERS = {ConfidenceTier.A, ConfidenceTier.B}


def _match_bucket(signal: Signal, buckets: list[EdgeBucket]) -> Optional[EdgeBucket]:
    exact = [
        b
        for b in buckets
        if b.entry_mode == signal.entry_mode
        and b.regime == signal.regime
        and b.hourly_bucket == signal.hourly_bucket
    ]
    if exact:
        return exact[0]
    # Soft match: mode + regime
    soft = [
        b
        for b in buckets
        if b.entry_mode == signal.entry_mode and b.regime == signal.regime
    ]
    return soft[0] if soft else None


def _lessons_avoid(signal: Signal, lessons: str) -> list[str]:
    hits: list[str] = []
    lower = lessons.lower()
    compact = lower.replace(" ", "")
    keys = [
        f"avoid:{signal.entry_mode.value}",
        f"avoid:{signal.regime.value}",
        f"avoid:hour={signal.hourly_bucket}",
        f"avoid:h{signal.hourly_bucket}",
    ]
    for k in keys:
        if k.replace(" ", "") in compact:
            hits.append(k)
    if signal.avoid_bucket_hit:
        hits.append("signal.avoid_bucket_hit=True")
    # Explicit osmani gate
    if signal.entry_mode == EntryMode.OSMANI_LANE:
        hits.append("osmani_lane gated by policy")
    return hits


def _sizing_ok(signal: Signal, state: dict) -> tuple[bool, float, str]:
    capital = float(state.get("capital_usd", state.get("capital", 10_000)) or 10_000)
    dd = float(state.get("max_drawdown_pct", state.get("drawdown_pct", 0)) or 0)
    open_exp = float(state.get("open_exposure_usd", 0) or 0)
    # Prefer portfolio-layer size when present
    suggested = signal.allocation_usd or signal.size_usd_suggested

    dd_scale = 1.0
    if dd >= MAX_PORTFOLIO_DD:
        return False, 0.0, f"portfolio DD {dd:.2%} >= {MAX_PORTFOLIO_DD:.0%}"
    if dd >= MAX_PORTFOLIO_DD * 0.5:
        dd_scale = max(0.25, 1.0 - (dd / MAX_PORTFOLIO_DD))

    max_usd = capital * MAX_SINGLE_POSITION_PCT * dd_scale
    sized = min(suggested, max_usd)

    if open_exp + sized > capital * MAX_CORRELATED_EXPOSURE_PCT * 2:
        return False, 0.0, "correlated/total exposure cap breached"

    if sized < 10:
        return False, 0.0, "sized below minimum ticket"

    return True, round(sized, 2), f"sized ${sized:.2f} (dd_scale={dd_scale:.2f})"


def _allocation_ok(
    signal: Signal,
    proposal: Optional[AllocationProposal],
) -> tuple[bool, str, str]:
    """Approve both signal AND proposed size/allocation (Ruuj layer)."""
    annotate_signal(signal)
    sid = signal.substrategy_id
    action = SubStrategyAction.HOLD.value

    if proposal is not None:
        if sid in proposal.cut_list:
            return False, "substrategy_CUT", SubStrategyAction.CUT.value
        if signal.allocation_usd <= 0 and signal.allocation_weight <= 0:
            return False, "zero_allocation_weight", action
        if proposal.concentration_hhi > MAX_HHI and signal.allocation_weight > 0.15:
            return (
                False,
                f"concentration_hhi={proposal.concentration_hhi:.3f}>{MAX_HHI}",
                action,
            )
        if (
            proposal.diversification_ratio < MIN_DIV_RATIO
            and len(proposal.weights) > 1
            and signal.allocation_weight > 0.20
        ):
            return (
                False,
                f"div_ratio={proposal.diversification_ratio:.3f}<{MIN_DIV_RATIO}",
                action,
            )
        if sid in proposal.reduce_list:
            action = SubStrategyAction.REDUCE.value
            # Still allow, but size must already be capped by allocator
            if signal.allocation_usd > 0:
                return True, f"REDUCE_ok size=${signal.allocation_usd:.2f}", action
            return False, "reduce_sleeve_zero_size", action
        return (
            True,
            f"alloc_w={signal.allocation_weight:.3f} ${signal.allocation_usd:.2f} "
            f"div={proposal.diversification_ratio:.2f}",
            action,
        )

    # No proposal yet — require non-toxic size suggestion only
    if signal.size_usd_suggested <= 0:
        return False, "no_size_suggested", action
    return True, "no_proposal_soft_pass", action


@checker(
    name="signal_verifier",
    model_hint="stronger",  # e.g. claude-opus / gpt-5.4 — NOT the generator model
    assume_broken_until_proven=True,
    criteria=[
        "bucket WR >= 65% with n >= 20",
        "live EV >= 0.06 after fees+slippage",
        "regime + conviction pass",
        "not in AVOID list",
        "sizing respects DD + correlation",
        "tier A/B only",
        "pre-entry stability + VWAP",
        "lane active",
        "allocation approved (HRP/BL size + concentration)",
        "sub-strategy not CUT",
    ],
)
def verify_signal(
    signal: Signal,
    *,
    buckets: Optional[list[EdgeBucket]] = None,
    state: Optional[dict] = None,
    lessons: Optional[str] = None,
    proposal: Optional[AllocationProposal] = None,
) -> VerificationReport:
    """Grade one signal + its allocation. Default: REJECT until every gate passes."""
    buckets = buckets if buckets is not None else load_edge_buckets_from_alpha()
    state = state if state is not None else parse_state_fields(read_state_md())
    lessons = lessons if lessons is not None else read_lessons_md()
    annotate_signal(signal)

    checks: list[CheckResult] = []
    rejections: list[str] = []

    # 0. Circuit / pause — coerce string flags carefully ("clear" must not trip)
    paused = bool(state.get("loop_paused") or state.get("pause_loop"))
    cb_raw = state.get("circuit_breaker", "clear")
    cb_tripped = str(cb_raw).lower() in {
        "tripped",
        "true",
        "1",
        "yes",
        "halt",
        "active",
    }
    if paused or cb_tripped:
        checks.append(
            CheckResult(
                name="circuit_breaker",
                passed=False,
                detail=f"pause={paused} circuit={cb_raw}",
            )
        )
        rejections.append("circuit_breaker")
    else:
        checks.append(
            CheckResult(
                name="circuit_breaker",
                passed=True,
                detail=f"pause={paused} circuit={cb_raw}",
            )
        )

    # 1. Lane status
    lane = LANE_STATUS.get(signal.entry_mode, LaneStatus.ACTIVE)
    lane_ok = lane == LaneStatus.ACTIVE or (
        lane == LaneStatus.PAPER_ONLY and bool(signal.meta.get("paper", True))
    )
    checks.append(
        CheckResult(
            name="lane_active",
            passed=lane_ok and lane not in (LaneStatus.GATED, LaneStatus.KILLED),
            detail=f"lane={lane.value}",
        )
    )
    if not checks[-1].passed:
        rejections.append(f"lane:{lane.value}")

    # 2. Confidence tier
    tier_ok = signal.confidence_tier in ALLOWED_TIERS
    checks.append(
        CheckResult(
            name="confidence_tier",
            passed=tier_ok,
            detail=f"tier={signal.confidence_tier.value}",
        )
    )
    if not tier_ok:
        rejections.append(f"tier:{signal.confidence_tier.value}")

    # 3. Historical bucket edge
    bucket = _match_bucket(signal, buckets)
    if bucket is None:
        # No history → DEFER to inbox rather than blind PASS
        checks.append(
            CheckResult(
                name="historical_bucket",
                passed=False,
                detail="no matching edge bucket — insufficient evidence",
            )
        )
        rejections.append("no_bucket_history")
        bucket_ok = False
    else:
        bucket_ok = (
            not bucket.avoid
            and bucket.sample_n >= MIN_BUCKET_N
            and bucket.win_rate >= MIN_BUCKET_WR
            and bucket.avg_edge >= MIN_BUCKET_EDGE
            and bucket.profit_factor >= MIN_PROFIT_FACTOR
            and bucket.max_drawdown <= MAX_BUCKET_DD
        )
        detail = (
            f"n={bucket.sample_n} wr={bucket.win_rate:.2%} edge={bucket.avg_edge:.3f} "
            f"pf={bucket.profit_factor:.2f} dd={bucket.max_drawdown:.2%} avoid={bucket.avoid}"
        )
        checks.append(
            CheckResult(name="historical_bucket", passed=bucket_ok, detail=detail)
        )
        if not bucket_ok:
            rejections.append("bucket_below_threshold")

    # 4. Live EV
    ev_ok = signal.live_ev >= MIN_LIVE_EV
    checks.append(
        CheckResult(
            name="live_ev",
            passed=ev_ok,
            detail=f"live_ev={signal.live_ev:.4f} (min={MIN_LIVE_EV})",
            weight=1.5,
        )
    )
    if not ev_ok:
        rejections.append(f"live_ev={signal.live_ev:.4f}")

    # 5. Regime + conviction
    regime_ok = signal.regime.value != "unknown"
    conv_ok = signal.conviction >= MIN_CONVICTION
    checks.append(
        CheckResult(
            name="regime_filter",
            passed=regime_ok,
            detail=f"regime={signal.regime.value}",
        )
    )
    checks.append(
        CheckResult(
            name="conviction",
            passed=conv_ok,
            detail=f"conviction={signal.conviction:.3f}",
        )
    )
    if not regime_ok:
        rejections.append("regime_unknown")
    if not conv_ok:
        rejections.append("low_conviction")

    # 6. AVOID lessons
    avoid_hits = _lessons_avoid(signal, lessons)
    avoid_ok = len(avoid_hits) == 0
    checks.append(
        CheckResult(
            name="avoid_list",
            passed=avoid_ok,
            detail="clean" if avoid_ok else f"hits={avoid_hits}",
            weight=2.0,
        )
    )
    if not avoid_ok:
        rejections.extend(avoid_hits)

    # 7. Pre-entry stability + VWAP (execution-drag fix)
    stable_ok = signal.pre_entry_stability_ok and signal.entry_vwap_target is not None
    checks.append(
        CheckResult(
            name="entry_quality",
            passed=stable_ok,
            detail=(
                f"stable={signal.pre_entry_stability_ok} "
                f"vwap={signal.entry_vwap_target}"
            ),
        )
    )
    if not stable_ok:
        rejections.append("entry_quality")

    # 8. Sizing / drawdown / correlation
    size_ok, sized, size_detail = _sizing_ok(signal, state)
    checks.append(
        CheckResult(name="position_sizing", passed=size_ok, detail=size_detail)
    )
    if not size_ok:
        rejections.append(size_detail)

    # 9. Allocation approval (signal + size must both clear)
    alloc_ok, alloc_detail, ss_action = _allocation_ok(signal, proposal)
    checks.append(
        CheckResult(
            name="allocation_approval",
            passed=alloc_ok,
            detail=alloc_detail,
            weight=1.5,
        )
    )
    if not alloc_ok:
        rejections.append(f"allocation:{alloc_detail}")

    # Score: weighted fraction of passed checks
    total_w = sum(c.weight for c in checks) or 1.0
    passed_w = sum(c.weight for c in checks if c.passed)
    score = passed_w / total_w

    all_pass = all(c.passed for c in checks)

    # Decision logic
    if all_pass:
        decision = VerifierDecision.PASS
    elif "no_bucket_history" in rejections and score >= 0.7:
        decision = VerifierDecision.DEFER
        push_inbox(
            {
                "type": "verify_defer",
                "signal_id": signal.signal_id,
                "market_id": signal.market_id,
                "reason": "no historical bucket — needs human/research confirmation",
                "score": score,
            }
        )
    else:
        decision = VerifierDecision.REJECT

    final_size = sized if decision == VerifierDecision.PASS else 0.0
    if decision == VerifierDecision.PASS and signal.allocation_usd > 0:
        final_size = min(sized, signal.allocation_usd)

    report = VerificationReport(
        signal_id=signal.signal_id,
        decision=decision,
        checks=checks,
        score=round(score, 4),
        rejection_reasons=rejections,
        sized_usd=final_size,
        allocation_weight=signal.allocation_weight,
        allocation_approved=alloc_ok and decision == VerifierDecision.PASS,
        substrategy_id=signal.substrategy_id,
        substrategy_action=ss_action,
        verifier_model="verifier-strong",  # swap to opus / o-series in prod
        notes=(
            "PASS — signal + allocation cleared"
            if decision == VerifierDecision.PASS
            else f"{decision.value}: {', '.join(rejections[:6])}"
        ),
    )

    logger.info(
        "verify %s → %s score=%.2f alloc=%s reasons=%s",
        signal.signal_id,
        report.decision.value,
        report.score,
        alloc_ok,
        report.rejection_reasons[:3],
    )
    return report


@loop(interval="5m", name="verifier")
def verifier_tick(
    signals: Optional[list[Signal]] = None,
    turn_id: Optional[str] = None,
    proposal: Optional[AllocationProposal] = None,
) -> list[VerificationReport]:
    """Verify a batch of signals + allocations. Only PASS reports proceed."""
    ensure_dirs()
    if signals is None:
        return []

    buckets = load_edge_buckets_from_alpha()
    state = parse_state_fields(read_state_md())
    lessons = read_lessons_md()

    reports = [
        verify_signal(
            s, buckets=buckets, state=state, lessons=lessons, proposal=proposal
        )
        for s in signals
    ]
    tid = turn_id or "adhoc"
    write_handoff("verifications", reports, tid)

    passed = sum(1 for r in reports if r.decision == VerifierDecision.PASS)
    rejected = sum(1 for r in reports if r.decision == VerifierDecision.REJECT)
    deferred = sum(1 for r in reports if r.decision == VerifierDecision.DEFER)
    logger.info(
        "verifier: %d in → %d PASS / %d REJECT / %d DEFER",
        len(reports),
        passed,
        rejected,
        deferred,
    )
    return reports
