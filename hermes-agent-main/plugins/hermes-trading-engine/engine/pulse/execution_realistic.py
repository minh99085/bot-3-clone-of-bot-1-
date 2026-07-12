"""Execution-realistic edge assembly (Roan Part IV + loop-engine reporting).

Per-candidate structured edge block and window-level simplex diagnostics. Report-only except
where the engine applies the margin-based high-entry guard before the execution gate.
PAPER ONLY.
"""

from __future__ import annotations

import math
from typing import Optional

from engine.pulse.execution_gate import vwap_fill


def kl_model_vs_market(p_model: float, p_market: float) -> Optional[float]:
    """KL((p,1-p) || (m,1-m)). Observe-only: large KL when model diverges from market."""
    try:
        eps = 1e-9
        p = max(eps, min(1.0 - eps, float(p_model)))
        m = max(eps, min(1.0 - eps, float(p_market)))
        return round(
            p * math.log(p / m) + (1.0 - p) * math.log((1.0 - p) / (1.0 - m)),
            6,
        )
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _reward_risk(ask: Optional[float]) -> Optional[dict]:
    if ask is None:
        return None
    try:
        p = float(ask)
        if p <= 0 or p >= 1:
            return None
        return {
            "ask": round(p, 4),
            "win_payoff_per_$": round((1.0 - p) / p, 4),
            "breakeven_win_rate": round(p, 4),
            "reward_to_risk": round((1.0 - p) / p, 4),
        }
    except (TypeError, ValueError):
        return None


def compute_candidate_edge(
    *,
    side: str,
    raw_fair_p: Optional[float],
    calibrated_fair_p: Optional[float],
    market_price: Optional[float],
    outcome_prob: Optional[float],
    book,
    size_usd: float,
    taker_fee_rate: float = 0.0,
    up_book=None,
    down_book=None,
) -> dict:
    """One structured execution-realistic block for a directional candidate."""
    best_ask = book.best_ask if book else None
    top_edge = None
    if outcome_prob is not None and best_ask is not None:
        p = float(best_ask)
        top_edge = round(float(outcome_prob) - p
                         - max(0.0, float(taker_fee_rate)) * p * (1.0 - p), 6)

    vwap = None
    slippage_bps = None
    depth_usd = float(book.ask_depth_usd if book else 0.0)
    fill_prob = None
    if book and best_ask is not None:
        vwap, filled_usd, _shares, fully = vwap_fill(book.asks or [], size_usd)
        if vwap is not None:
            slippage_bps = round((vwap - best_ask) / best_ask * 10000.0, 2) if best_ask else None
        fill_prob = 1.0 if fully else round(filled_usd / size_usd, 4) if size_usd else None

    cal_p = calibrated_fair_p if calibrated_fair_p is not None else raw_fair_p
    breakeven = float(best_ask) if best_ask is not None else None
    cal_margin = None
    if cal_p is not None and breakeven is not None:
        oprob = float(cal_p) if side == "up" else (1.0 - float(cal_p))
        fee_ps = max(0.0, float(taker_fee_rate)) * breakeven * (1.0 - breakeven)
        cal_margin = round(oprob - breakeven - fee_ps, 6)

    max_win = None
    max_loss = None
    exec_ev = None
    if vwap is not None and outcome_prob is not None and size_usd:
        shares = size_usd / vwap
        max_win = round(shares * (1.0 - vwap), 4)
        max_loss = round(-size_usd, 4)
        fee_usd = shares * max(0.0, float(taker_fee_rate)) * vwap * (1.0 - vwap)
        exec_ev = round((float(outcome_prob) - vwap) * shares - fee_usd, 4)

    rr = _reward_risk(best_ask)
    kl = None
    if cal_p is not None and market_price is not None:
        kl = kl_model_vs_market(cal_p, market_price)

    simplex = simplex_diagnostics(up_book, down_book, size_usd)

    return {
        "side": side,
        "raw_fair_p": raw_fair_p,
        "calibrated_fair_p": calibrated_fair_p,
        "market_price": market_price,
        "top_of_book_edge": top_edge,
        "vwap_entry_price": (round(vwap, 6) if vwap is not None else None),
        "slippage_bps": slippage_bps,
        "depth_available_usd": round(depth_usd, 2),
        "expected_fill_probability": fill_prob,
        "max_win_profit_usd": max_win,
        "max_loss_usd": max_loss,
        "reward_to_risk": (rr or {}).get("reward_to_risk"),
        "breakeven_probability": breakeven,
        "calibrated_probability_margin": cal_margin,
        "execution_realistic_ev": exec_ev,
        "taker_fee_rate": round(max(0.0, float(taker_fee_rate)), 6),
        "kl_model_vs_market": kl,
        "simplex": simplex,
    }


def simplex_diagnostics(up_book, down_book, size_usd: float) -> dict:
    """Binary simplex residuals: top-of-book and VWAP ask sums (arb signal diagnostics)."""
    up_ask = up_book.best_ask if up_book else None
    down_ask = down_book.best_ask if down_book else None
    up_bid = up_book.best_bid if up_book else None
    down_bid = down_book.best_bid if down_book else None

    tob_ask_sum = None
    tob_ask_residual = None
    if up_ask is not None and down_ask is not None:
        tob_ask_sum = round(float(up_ask) + float(down_ask), 6)
        tob_ask_residual = round(abs(tob_ask_sum - 1.0), 6)

    vwap_up = vwap_down = None
    vwap_ask_sum = vwap_ask_residual = None
    if up_book and down_book:
        vwap_up, _, _, fu = vwap_fill(up_book.asks or [], size_usd)
        vwap_down, _, _, fd = vwap_fill(down_book.asks or [], size_usd)
        if vwap_up is not None and vwap_down is not None and fu and fd:
            vwap_ask_sum = round(vwap_up + vwap_down, 6)
            vwap_ask_residual = round(abs(vwap_ask_sum - 1.0), 6)

    tob_bid_sum = None
    tob_bid_residual = None
    if up_bid is not None and down_bid is not None:
        tob_bid_sum = round(float(up_bid) + float(down_bid), 6)
        tob_bid_residual = round(abs(tob_bid_sum - 1.0), 6)

    return {
        "tob_ask_sum": tob_ask_sum,
        "abs_tob_ask_residual": tob_ask_residual,
        "vwap_ask_sum": vwap_ask_sum,
        "abs_vwap_ask_residual": vwap_ask_residual,
        "tob_bid_sum": tob_bid_sum,
        "abs_tob_bid_residual": tob_bid_residual,
        "buy_both_arb_signal": (
            vwap_ask_sum is not None and vwap_ask_sum < 1.0 - 1e-6
        ),
        "sell_both_arb_signal": (
            tob_bid_sum is not None and tob_bid_sum > 1.0 + 1e-6
        ),
    }


def high_entry_margin_reject(
    *,
    ask: Optional[float],
    calibrated_prob: Optional[float],
    min_margin: float = 0.04,
    high_entry_threshold: float = 0.75,
) -> Optional[str]:
    """Reject expensive entries unless calibrated P(win) clears breakeven by ``min_margin``."""
    if ask is None or calibrated_prob is None:
        return None
    try:
        p = float(ask)
        if p < high_entry_threshold:
            return None
        margin = float(calibrated_prob) - p
        if margin < float(min_margin):
            return "high_entry_insufficient_margin"
    except (TypeError, ValueError):
        return None
    return None


def max_acceptable_leg2_ask(
    *,
    leg1_vwap: float,
    epsilon: float = 0.05,
    fees: float = 0.0,
) -> float:
    """Pre-commit ceiling for leg-2 ask (buy-both): leg1 + leg2 must stay below 1 - fees - ε."""
    return round(1.0 - float(fees) - float(epsilon) - float(leg1_vwap), 6)


def _consume_asks(levels: list, notional: float) -> list:
    """Shallow-copied ask ladder with ``notional`` USD consumed from the front."""
    out = []
    remaining = float(notional)
    for px, sz in levels or []:
        px, sz = float(px), float(sz)
        lvl_usd = px * sz
        if remaining <= 0:
            out.append((px, sz))
            continue
        if lvl_usd <= remaining + 1e-9:
            remaining -= lvl_usd
            continue
        keep_sh = (lvl_usd - remaining) / px
        out.append((px, keep_sh))
        remaining = 0.0
    return out


def simulate_buy_both_sequential(
    up_book,
    down_book,
    *,
    target_usd: float,
    fees: float = 0.0,
    epsilon: float = 0.05,
    leg2_slippage_bps: float = 50.0,
    leg_order: str = "up_first",
    max_book_age_s: float = 30.0,
    now: Optional[float] = None,
) -> dict:
    """Multi-leg non-atomic simulation with pre-commit leg-2 max and unwind flag.

    Fills leg 1, stresses leg 2 (impact + slippage), rejects if profit cannot survive.
    """

    leg_order = (leg_order or "up_first").lower()
    first_book, second_book = (up_book, down_book) if leg_order == "up_first" else (down_book, up_book)
    first_asks = list(getattr(first_book, "asks", None) or [])
    second_asks = list(getattr(second_book, "asks", None) or [])
    if not first_asks or not second_asks:
        return {"non_atomic_pass": False, "reason": "missing_book", "unwind_required": False}

    v1, spent1, sh1, full1 = vwap_fill(first_asks, target_usd)
    if v1 is None or not full1 or sh1 <= 0:
        return {"non_atomic_pass": False, "reason": "leg1_partial_or_empty", "unwind_required": False}

    pre_commit_max = max_acceptable_leg2_ask(leg1_vwap=v1, epsilon=epsilon, fees=fees)
    impacted = _consume_asks(second_asks, spent1)
    slip_mult = 1.0 + float(leg2_slippage_bps) / 10000.0
    stressed = [(round(px * slip_mult, 6), sz) for px, sz in impacted]

    best_second = float(stressed[0][0]) if stressed else None
    leg2_breach = bool(best_second is not None and best_second > pre_commit_max + 1e-9)

    v2, spent2, sh2, full2 = vwap_fill(stressed, target_usd)
    if v2 is None or not full2:
        return {
            "non_atomic_pass": False,
            "reason": "leg2_partial_after_impact",
            "leg_order_tested": leg_order,
            "leg1_vwap": round(v1, 6),
            "pre_commit_leg2_max": pre_commit_max,
            "leg2_breach_pre_commit": leg2_breach,
            "unwind_required": True,
            "worst_case_profit_usd": None,
        }

    shares = min(sh1, sh2)
    ask_sum = v1 + v2
    profit = shares * (1.0 - ask_sum)
    threshold = 1.0 - float(fees) - float(epsilon)
    survives = bool(shares > 0 and ask_sum < threshold and profit > 0 and not leg2_breach)

    reason = "ok" if survives else (
        "leg2_pre_commit_breach" if leg2_breach else
        ("below_epsilon_after_nonatomic" if ask_sum >= threshold else "nonatomic_profit_gone"))

    return {
        "non_atomic_pass": survives,
        "survives": survives,
        "reason": reason,
        "shares": round(shares, 4),
        "leg_order_tested": leg_order,
        "leg1_vwap": round(v1, 6),
        "leg2_vwap": round(v2, 6),
        "pre_commit_leg2_max": pre_commit_max,
        "leg2_breach_pre_commit": leg2_breach,
        "ask_sum": round(ask_sum, 6),
        "guaranteed_profit_usd": round(profit, 4),
        "worst_case_profit_usd": round(profit, 4),
        "partial_fill_exposure": round(spent1, 4) if not survives else 0.0,
        "unwind_required": bool(not survives and full1),
        "leg2_stressed": True,
        "leg2_slippage_bps": leg2_slippage_bps,
    }


def aggregate_report(
    *,
    samples: list,
    payoff_guards: dict,
    kl_aggregate: Optional[dict] = None,
) -> dict:
    """Roll up per-candidate samples into the light-report section."""
    n = len(samples)
    avg_ev = None
    evs = [s.get("execution_realistic_ev") for s in samples if s.get("execution_realistic_ev") is not None]
    if evs:
        avg_ev = round(sum(evs) / len(evs), 6)
    avg_kl = None
    kls = [s.get("kl_model_vs_market") for s in samples if s.get("kl_model_vs_market") is not None]
    if kls:
        avg_kl = round(sum(kls) / len(kls), 6)
    return {
        "observe_only": True,
        "affects_trading": False,
        "candidates_scored": n,
        "avg_execution_realistic_ev_usd": avg_ev,
        "avg_kl_model_vs_market": avg_kl,
        "kl_model_vs_market": kl_aggregate or {},
        "payoff_guards": dict(payoff_guards or {}),
        "recent_samples": samples[-12:],
        "note": ("KL divergence is observe-only — large KL means model disagrees with market; "
                 "never a buy signal when model Brier > market Brier."),
    }
