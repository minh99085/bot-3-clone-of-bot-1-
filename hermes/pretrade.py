"""Pre-trade analysis & sizing — Handoff step before Verifier.

Before any trade is proposed for execution:
  1. Pull sleeve performance from ledger/state
  2. Read LESSONS.md for binding CUT/REDUCE/AVOID rules
  3. Recalculate live EV from orderbook + Chainlink context
  4. Assess portfolio impact (div ratio, concentration)
  5. Apply HRP/BL allocation weight → recommended % of bankroll (or 0% skip)

Verifier must approve both signal quality and this proposed size.
All decisions are logged to the paper ledger for the dashboard.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from hermes.models import (
    AllocationProposal,
    PreTradeAnalysis,
    Signal,
    SubStrategyAction,
)
from hermes.state_io import (
    append_jsonl,
    ensure_dirs,
    ledger_path,
    parse_state_fields,
    read_jsonl,
    read_lessons_md,
    read_state_md,
    write_handoff,
)
from hermes.substrategy import annotate_signal, default_confidence

logger = logging.getLogger(__name__)

# Bankroll-aware sizing caps ($2000 starting paper)
DEFAULT_BANKROLL = 2000.0
MAX_SIZE_PCT = 0.03  # 3% of bankroll per ticket (slow markets)
MIN_SIZE_PCT = 0.005  # 0.5% floor when trading
MIN_TICKET_USD = 10.0
MIN_LIVE_EV = 0.06
MIN_SLEEVE_WR = 0.55
MAX_CONCENTRATION = 0.40
FEE_BPS = 100.0
SLIPPAGE_BPS_FLOOR = 40.0


def _fast_btc_scope(signal: Signal) -> bool:
    from hermes.market_scope import is_allowed_series, is_allowed_slug, scope_enabled

    if not scope_enabled():
        return False
    if is_allowed_slug(signal.slug):
        return True
    return is_allowed_series(signal.market_series)


def _sizing_ladder(stats: dict, live_ev: float, lessons: str) -> tuple[float, float, str]:
    """Cold-start small → scale only when WR/EV lessons prove out.

    Returns (max_size_pct, min_live_ev, note).
    """
    from hermes.market_scope import (
        COLD_START_SIZE_PCT,
        MAX_SIZE_PCT_FAST,
        MIN_LIVE_EV_FAST,
        MIN_SIZE_PCT_FAST,
    )

    n = int(stats.get("n") or 0)
    wr = float(stats.get("wr") or 0.0)
    ev = float(stats.get("ev") or 0.0)
    lower = lessons.lower()

    # Lesson-driven aggression
    aggressive = "aggressive:" in lower or "size_up:" in lower or "boost size" in lower
    conservative = "conservative:" in lower or "size_down:" in lower or "cut size" in lower

    if n < 5:
        return COLD_START_SIZE_PCT, MIN_LIVE_EV_FAST, "cold_start_0.5%"
    if n < 15 or wr < 0.60 or ev < 0.02:
        cap = MIN_SIZE_PCT_FAST * 1.5  # ~0.75%
        if conservative:
            cap = MIN_SIZE_PCT_FAST
        return cap, MIN_LIVE_EV_FAST, f"early_n={n}_wr={wr:.0%}"
    if wr >= 0.75 and ev >= 0.04 and live_ev >= 0.05:
        cap = MAX_SIZE_PCT_FAST
        if aggressive:
            cap = min(0.025, MAX_SIZE_PCT_FAST * 1.25)
        if conservative:
            cap = MAX_SIZE_PCT_FAST * 0.6
        return cap, 0.035, f"proven_wr={wr:.0%}_ev={ev:.3f}"
    if wr >= 0.65 and ev >= 0.02:
        return 0.012, MIN_LIVE_EV_FAST, f"building_wr={wr:.0%}"
    return MIN_SIZE_PCT_FAST, MIN_LIVE_EV_FAST + 0.01, f"cautious_wr={wr:.0%}"


def _bankroll(state: Optional[dict] = None) -> float:
    state = state if state is not None else parse_state_fields(read_state_md())
    return float(
        state.get("capital_usd")
        or state.get("capital")
        or state.get("starting_bankroll_usd")
        or DEFAULT_BANKROLL
    )


def _sleeve_stats(substrategy_id: str, paper: bool = True) -> dict:
    rows = read_jsonl(ledger_path(paper=paper))
    settles = [
        r
        for r in rows
        if (r.get("event") == "settlement" or r.get("won") is not None)
        and (
            r.get("substrategy_id") == substrategy_id
            or substrategy_id in str(r.get("applies_to", ""))
            or str(r.get("market_series", "")) == substrategy_id
            or str(r.get("substrategy_id", "")).startswith(substrategy_id + "|")
        )
    ]
    if not settles:
        # Soft match on mode|regime fragment
        parts = substrategy_id.split("|")
        if len(parts) >= 3:
            mode, regime = parts[1], parts[2]
            settles = [
                r
                for r in rows
                if (r.get("event") == "settlement" or r.get("won") is not None)
                and str(r.get("entry_mode", "")) == mode
                and str(r.get("regime", "")) == regime
            ]
    if not settles:
        return {"n": 0, "wr": 0.70, "ev": 0.06, "currently_losing": False}
    wins = sum(1 for r in settles if r.get("won") or float(r.get("pnl_usd", 0)) > 0)
    rets = []
    for r in settles:
        sz = float(r.get("size_usd", 1) or 1)
        rets.append(float(r.get("pnl_usd", 0)) / sz)
    last3 = settles[-3:]
    losing = all(not (r.get("won") or float(r.get("pnl_usd", 0)) > 0) for r in last3)
    return {
        "n": len(settles),
        "wr": wins / len(settles),
        "ev": sum(rets) / len(rets) if rets else 0.0,
        "currently_losing": losing and len(last3) >= 3,
    }


def _lessons_for_sleeve(substrategy_id: str, lessons: str) -> list[str]:
    """Extract actionable lesson rule lines that bind this sleeve."""
    hits: list[str] = []
    lower = lessons.lower()
    parts = substrategy_id.lower().split("|")
    mode = parts[1] if len(parts) > 1 else ""
    for block in re.split(r"\n### ", lessons):
        blob = block.lower()
        if "retired**: true" in blob:
            continue
        rule_m = re.search(r"\*\*rule\*\*:\s*(.+)", block, re.I)
        if not rule_m:
            continue
        rule = rule_m.group(1).strip()
        rl = rule.lower()
        if substrategy_id.lower() in blob or substrategy_id.lower() in rl:
            hits.append(rule)
            continue
        if mode and (f"avoid:{mode}" in rl.replace(" ", "") or f"cut:`{mode}" in rl):
            hits.append(rule)
            continue
        if "cut:" in rl and any(p in rl for p in parts[:2]):
            hits.append(rule)
        if "reduce weight" in rl and mode and mode in rl:
            hits.append(rule)
    # Cap noise
    return hits[:8]


def _recalc_live_ev(signal: Signal) -> tuple[float, float, str]:
    """Recalc EV after cost using orderbook slip when possible + Chainlink context."""
    slip_bps = SLIPPAGE_BPS_FLOOR
    note = "floor_slip"
    token = signal.clob_token_id
    if token:
        try:
            from connectors.polymarket import PolymarketClient

            pm = PolymarketClient()
            if signal.direction.value in ("YES", "UP"):
                _, slip_bps = pm.simulate_buy_vwap(token, max(25.0, signal.size_usd_suggested))
            else:
                _, slip_bps = pm.simulate_buy_vwap(token, max(25.0, signal.size_usd_suggested))
            note = f"book_slip={slip_bps:.1f}bps"
        except Exception as exc:  # noqa: BLE001
            note = f"book_unavailable:{exc}"
    # Oracle alignment soft-adjusts EV for crypto HF
    align = float(signal.oracle_alignment or 0.5)
    adj = 0.0
    if signal.timeframe in ("5m", "15m") and (signal.meta or {}).get("asset"):
        adj = (align - 0.5) * 0.04  # ±2% EV tilt from alignment
        note += f" oracle_align={align:.2f}"
    cost = (FEE_BPS + max(SLIPPAGE_BPS_FLOOR, slip_bps)) / 10_000.0
    live_ev = float(signal.expected_edge) - cost + adj
    return live_ev, slip_bps, note


def _max_slip_bps() -> float:
    """Hard entry-slippage ceiling (bps vs mid). Default 150."""
    try:
        return float(os.environ.get("HERMES_MAX_SLIP_BPS", "150"))
    except ValueError:
        return 150.0


def analyze_signal(
    signal: Signal,
    proposal: AllocationProposal,
    *,
    bankroll: float,
    lessons: Optional[str] = None,
    paper: bool = True,
) -> PreTradeAnalysis:
    """Produce a size recommendation (% of bankroll) or skip (0%)."""
    annotate_signal(signal)

    # PURE mode (B1): fixed sizing, adaptive layers (lessons/ladder/sleeve
    # stats/bandit/kelly/HHI) all bypassed. Scope check + hard cap + min
    # ticket stay — they are safety rails, not adaptivity.
    from hermes.pure_mode import pure_fixed_size_pct, pure_mode_enabled

    if pure_mode_enabled():
        reasons = ["pure_mode: fixed size, adaptive stack disabled"]
        skip = False
        if _fast_btc_scope(signal):
            from hermes.market_scope import is_allowed_slug

            if not is_allowed_slug(signal.slug) and signal.market_series not in (
                "btc_updown_5m",
                "btc_updown_15m",
            ):
                skip = True
                reasons.append(f"out_of_scope_slug={signal.slug}")
        live_ev, slip_bps_pure, ev_note = _recalc_live_ev(signal)
        reasons.append(ev_note)
        if slip_bps_pure > _max_slip_bps():
            skip = True
            reasons.append(f"slippage_gate {slip_bps_pure:.0f}bps>{_max_slip_bps():.0f}bps")
        hard_cap = float(os.environ.get("HERMES_MAX_TRADE_PCT", "0.02"))
        size_pct = min(pure_fixed_size_pct(), hard_cap)
        size_usd = round(bankroll * size_pct, 2) if not skip else 0.0
        if not skip and size_usd < MIN_TICKET_USD:
            if bankroll * size_pct >= MIN_TICKET_USD * 0.8:
                size_usd = MIN_TICKET_USD
                size_pct = size_usd / bankroll
                reasons.append("bumped_to_min_ticket_$10")
            else:
                skip = True
                size_pct = 0.0
                size_usd = 0.0
                reasons.append(f"ticket below ${MIN_TICKET_USD} → skip")
        return PreTradeAnalysis(
            signal_id=signal.signal_id,
            substrategy_id=signal.substrategy_id,
            bankroll_usd=bankroll,
            recommended_size_pct=round(size_pct * 100, 3) if not skip else 0.0,
            recommended_size_usd=size_usd,
            skip=skip,
            live_ev=round(live_ev, 5),
            sleeve_wr=0.0,
            sleeve_ev=0.0,
            sleeve_n=0,
            portfolio_div_before=round(proposal.diversification_ratio, 4),
            portfolio_div_after=round(proposal.diversification_ratio, 4),
            concentration_after=round(proposal.concentration_hhi, 4),
            allocation_weight=0.0,
            lessons_applied=[],
            reasons=reasons,
            oracle_alignment=float(signal.oracle_alignment or 0.5),
            mispricing_active=bool((signal.meta or {}).get("mispricing_active")),
            mispricing_dislocation=float(
                (signal.meta or {}).get("mispricing_dislocation") or 0
            ),
            bandit_arm=str((signal.meta or {}).get("bandit_arm") or ""),
            bandit_context=str((signal.meta or {}).get("bandit_context") or ""),
            entry_source=str((signal.meta or {}).get("entry_source") or "baseline"),
        )

    lessons = lessons if lessons is not None else read_lessons_md()
    sid = signal.substrategy_id
    stats = _sleeve_stats(sid, paper=paper)
    # Also pull series-level stats for fast BTC (across windows)
    series_stats = _sleeve_stats(signal.market_series, paper=paper) if signal.market_series else stats
    if series_stats["n"] > stats["n"]:
        # Prefer series experience on rotating windows
        merged = {
            "n": series_stats["n"],
            "wr": series_stats["wr"],
            "ev": series_stats["ev"],
            "currently_losing": series_stats["currently_losing"] or stats["currently_losing"],
        }
    else:
        merged = stats

    lesson_hits = _lessons_for_sleeve(sid, lessons)
    # Also match series-level lessons
    if signal.market_series:
        for extra in _lessons_for_sleeve(signal.market_series, lessons):
            if extra not in lesson_hits:
                lesson_hits.append(extra)

    from hermes.market_scope import is_rotator

    if is_rotator():
        # Ignore circular meta-lessons from prior bad paper settlement cycles.
        lesson_hits = [
            r
            for r in lesson_hits
            if "ALLOCATION_REJECT" not in r and "pretrade_skip" not in r.lower()
        ]

    live_ev, slip_bps, ev_note = _recalc_live_ev(signal)
    fast = _fast_btc_scope(signal)

    reasons: list[str] = []
    skip = False
    size_pct = 0.0

    # Slippage gate — the last-10h fleet paid 389bps AVERAGE entry slippage
    # (max 3143bps): walking thin books taxes away any edge. Skip entries the
    # book can't absorb near the touch instead of paying up for them.
    if slip_bps > _max_slip_bps():
        skip = True
        reasons.append(f"slippage_gate {slip_bps:.0f}bps>{_max_slip_bps():.0f}bps")

    if fast:
        from hermes.market_scope import MIN_ORACLE_ALIGN, is_allowed_slug

        if not is_allowed_slug(signal.slug) and signal.market_series not in (
            "btc_updown_5m",
            "btc_updown_15m",
        ):
            skip = True
            reasons.append(f"out_of_scope_slug={signal.slug}")
        max_pct, min_ev, ladder_note = _sizing_ladder(merged, live_ev, lessons)
        reasons.append(f"fast_ladder:{ladder_note}")
    else:
        max_pct, min_ev = MAX_SIZE_PCT, MIN_LIVE_EV

    # Binding lesson cuts — series/sleeve scoped (no cross-asset mispricing blocks)
    for rule in lesson_hits:
        rl = rule.lower()
        series = signal.market_series or ""
        if rl.startswith("cut:") or "weight cap=0" in rl or "model_broken" in rl:
            if sid in rule or (series and f"`{series}`" in rule):
                skip = True
                reasons.append(f"lesson_CUT: {rule[:120]}")
        if "avoid:" in rl[:40]:
            if series and f"`{series}`" in rule:
                if "until" in rl or "gated" in rl or "skip" in rl:
                    skip = True
                    reasons.append(f"lesson_AVOID: {rule[:120]}")
            elif sid in rule:
                skip = True
                reasons.append(f"lesson_AVOID: {rule[:120]}")
        if "skip:" in rl and series and f"`{series}`" in rule:
            skip = True
            reasons.append(f"lesson_SKIP: {rule[:120]}")
        if rl.startswith("reduce:") and (sid in rule or (series and f"`{series}`" in rule)):
            reasons.append(f"lesson_REDUCE: {rule[:80]}")
            # REDUCE halves size later via conf_scale; do not hard-skip

    if sid in proposal.cut_list:
        skip = True
        reasons.append("allocation_policy: sleeve on CUT list")

    if live_ev < min_ev:
        # Mispricing explore may clear a softer floor later; still skip if terrible
        meta0 = signal.meta or {}
        if meta0.get("mispricing_active") and meta0.get("bandit_arm") == "explore" and live_ev >= 0.03:
            reasons.append(f"explore_ev_soft_pass live_ev={live_ev:.4f}")
        else:
            skip = True
            reasons.append(f"live_ev={live_ev:.4f}<{min_ev} ({ev_note})")

    wr_floor = 0.50 if fast else MIN_SLEEVE_WR
    n_floor = 8 if fast else 10
    if merged["n"] >= n_floor and merged["wr"] < wr_floor:
        skip = True
        reasons.append(f"sleeve_wr={merged['wr']:.2%} n={merged['n']} below {wr_floor:.0%}")

    align_floor = 0.55 if fast else 0.45
    meta_pre = signal.meta or {}
    if meta_pre.get("mispricing_active") and meta_pre.get("sources_agree", True):
        align_floor = 0.35  # CEX lead setups may diverge briefly from PM-implied align
    if signal.oracle_alignment < align_floor and signal.timeframe in ("5m", "15m"):
        skip = True
        reasons.append(f"oracle_alignment={signal.oracle_alignment:.2f} too low for HF")

    # Portfolio impact
    w = float(proposal.weights.get(sid, signal.allocation_weight or 0.0))
    if sid in proposal.reduce_list:
        w = min(w, 0.08)
        reasons.append("REDUCE cap applied (≤8% sleeve weight)")

    div_before = proposal.diversification_ratio
    weights = dict(proposal.weights)
    weights[sid] = max(weights.get(sid, 0.0), w if w > 0 else 0.5)
    total = sum(weights.values()) or 1.0
    norm = {k: v / total for k, v in weights.items()}
    hhi_after = sum(v * v for v in norm.values())
    div_after = div_before * (1.0 - 0.15 * max(0.0, hhi_after - proposal.concentration_hhi))

    # With only 2 allowed markets, HHI≈0.5–1.0 is expected — do not block
    max_hhi = 0.85 if fast else MAX_CONCENTRATION
    if (not fast) and hhi_after > max_hhi and w > 0.12:
        skip = True
        reasons.append(f"concentration_hhi={hhi_after:.3f}>{max_hhi}")

    if not skip:
        edge_scale = min(1.5, max(0.4, live_ev / 0.08))
        conf = default_confidence(sid, signal)
        conf_scale = 0.5 + 0.5 * conf.internal_confidence
        if merged["currently_losing"]:
            conf_scale *= 0.5
            reasons.append("currently_losing → half size")
        if sid in proposal.reduce_list:
            conf_scale *= 0.5

        # Option D — bandit arm modulates size / skip
        meta = signal.meta or {}
        bandit_arm = str(meta.get("bandit_arm") or "")
        bandit_scale = float(meta.get("bandit_size_scale") or 0.0)
        mp_active = bool(meta.get("mispricing_active"))
        if bandit_arm == "skip":
            skip = True
            size_pct = 0.0
            reasons.append(f"bandit_SKIP: {meta.get('bandit_reason', '')[:100]}")
        elif fast:
            size_pct = max_pct * edge_scale * conf_scale
            size_pct = min(max_pct, max(max_pct * 0.5, size_pct))
            if merged["n"] < 5:
                size_pct = max_pct
            if bandit_arm == "explore":
                size_pct = min(size_pct, max_pct * 0.5)
                reasons.append("bandit_EXPLORE half-size probe")
            elif bandit_arm == "exploit" and mp_active:
                size_pct = min(max_pct * 1.5, size_pct * max(1.0, bandit_scale))
                reasons.append(f"bandit_EXPLOIT scale={bandit_scale:.2f}")
            if mp_active:
                reasons.append(
                    f"mispricing_disloc={float(meta.get('mispricing_dislocation') or 0):+.3f}"
                )
            # Enhanced Kelly override when Bayesian hard filter passed
            if meta.get("enhanced_passes") and float(meta.get("kelly_f") or 0) > 0:
                kelly_pct = min(0.10, float(meta["kelly_f"]))
                size_pct = kelly_pct
                reasons.append(
                    f"enhanced_kelly f*={meta.get('kelly_f_star')} "
                    f"f={meta.get('kelly_f')} kappa={meta.get('kelly_kappa')}"
                )
        else:
            size_pct = min(
                MAX_SIZE_PCT,
                max(0.0, proposal.weights.get(sid, w) * 0.15 * edge_scale * conf_scale),
            )
            if size_pct < MIN_SIZE_PCT:
                size_pct = 0.0
                skip = True
                reasons.append("size_below_min_pct → skip")

        if not skip:
            reasons.append(
                f"size_pct={size_pct:.2%} max={max_pct:.2%} edge_scale={edge_scale:.2f} "
                f"conf_scale={conf_scale:.2f} ({ev_note})"
            )

    # HARD per-trade cap — no multiplier path (ladder boosts, bandit exploit
    # scaling, kelly path) may exceed it. Oversized tickets ($300 = 15% of a
    # $2k lane) turned routine longshot streaks into instant hard-DD lockouts.
    hard_cap = float(os.environ.get("HERMES_MAX_TRADE_PCT", "0.02"))
    if size_pct > hard_cap:
        reasons.append(f"hard_cap {size_pct:.2%}→{hard_cap:.2%}")
        size_pct = hard_cap

    size_usd = round(bankroll * size_pct, 2) if not skip else 0.0
    if not skip and size_usd < MIN_TICKET_USD:
        # Round up to min ticket if within 20% of floor for cold-start
        if fast and bankroll * max_pct >= MIN_TICKET_USD * 0.8:
            size_usd = MIN_TICKET_USD
            size_pct = size_usd / bankroll
            reasons.append("bumped_to_min_ticket_$10")
        else:
            skip = True
            size_pct = 0.0
            size_usd = 0.0
            reasons.append(f"ticket below ${MIN_TICKET_USD} → skip")

    analysis = PreTradeAnalysis(
        signal_id=signal.signal_id,
        substrategy_id=sid,
        bankroll_usd=bankroll,
        recommended_size_pct=round(size_pct * 100, 3) if not skip else 0.0,
        recommended_size_usd=size_usd,
        skip=skip,
        live_ev=round(live_ev, 5),
        sleeve_wr=round(merged["wr"], 4),
        sleeve_ev=round(merged["ev"], 5),
        sleeve_n=int(merged["n"]),
        portfolio_div_before=round(div_before, 4),
        portfolio_div_after=round(div_after, 4),
        concentration_after=round(hhi_after, 4),
        allocation_weight=round(w, 4),
        lessons_applied=lesson_hits,
        reasons=reasons,
        oracle_alignment=float(signal.oracle_alignment or 0.5),
        mispricing_active=bool((signal.meta or {}).get("mispricing_active")),
        mispricing_dislocation=float((signal.meta or {}).get("mispricing_dislocation") or 0),
        bandit_arm=str((signal.meta or {}).get("bandit_arm") or ""),
        bandit_context=str((signal.meta or {}).get("bandit_context") or ""),
        entry_source=str((signal.meta or {}).get("entry_source") or "baseline"),
    )
    return analysis


def apply_pretrade_to_signal(signal: Signal, analysis: PreTradeAnalysis) -> Signal:
    signal.pretrade_analysis_id = analysis.analysis_id
    signal.pretrade_skip = analysis.skip
    signal.pretrade_reasons = list(analysis.reasons)
    signal.size_pct_recommended = analysis.recommended_size_pct
    signal.live_ev = analysis.live_ev
    if analysis.skip:
        signal.allocation_usd = 0.0
        signal.size_usd_suggested = 0.0
        signal.allocation_weight = 0.0
    else:
        signal.allocation_usd = analysis.recommended_size_usd
        signal.size_usd_suggested = analysis.recommended_size_usd
        signal.allocation_weight = analysis.allocation_weight
    return signal


def run_pretrade_sizing(
    signals: list[Signal],
    proposal: AllocationProposal,
    *,
    turn_id: str,
    paper: bool = True,
) -> tuple[list[Signal], list[PreTradeAnalysis]]:
    """Handoff sizing step: analyze each signal, log decisions, return sized set."""
    ensure_dirs()
    state = parse_state_fields(read_state_md())
    bankroll = _bankroll(state)
    lessons = read_lessons_md()
    analyses: list[PreTradeAnalysis] = []
    sized: list[Signal] = []

    for sig in signals:
        analysis = analyze_signal(
            sig, proposal, bankroll=bankroll, lessons=lessons, paper=paper
        )
        updated = apply_pretrade_to_signal(sig.model_copy(deep=True), analysis)
        analyses.append(analysis)
        sized.append(updated)
        append_jsonl(
            ledger_path(paper=paper).parent / "pretrade_decisions.jsonl",
            {
                "event": "pretrade",
                **analysis.model_dump(mode="json"),
                "market_series": updated.market_series,
                "slug": updated.slug,
                "timeframe": updated.timeframe,
            },
        )
        logger.info(
            "pretrade %s → %s $%.2f (%.2f%%) skip=%s :: %s",
            analysis.substrategy_id,
            "SKIP" if analysis.skip else "SIZE",
            analysis.recommended_size_usd,
            analysis.recommended_size_pct,
            analysis.skip,
            "; ".join(analysis.reasons[:2]),
        )

    # Refresh proposal sizes from pretrade
    proposal.signal_sizes_usd = {
        s.signal_id: s.allocation_usd for s in sized
    }
    write_handoff("pretrade", analyses, turn_id)
    write_handoff("signals_sized", sized, turn_id)
    return sized, analyses
