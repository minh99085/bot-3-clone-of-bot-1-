"""Bayesian conviction via Beta distribution (scipy.stats.beta).

Model belief:  q ~ approx with Beta(α, β) where
  α = q * n_eff
  β = (1 - q) * n_eff

For a YES bet at market price p:
  conviction = 1 - BetaCDF(p; α, β)
  = P(true_prob > p | model)

For a NO bet the conviction is symmetric:
  conviction = BetaCDF(p; α, β) = P(true_prob < p | model)
"""

from __future__ import annotations

from dataclasses import dataclass

from scipy.stats import beta as beta_dist


@dataclass(frozen=True)
class BayesianConviction:
    alpha: float
    beta: float
    n_eff: float
    conviction: float
    side: str


def beta_params(q: float, n_eff: float) -> tuple[float, float]:
    """Map point estimate q and effective sample size → Beta(α, β).

    α = q * n_eff,  β = (1 - q) * n_eff
    Clamped so α, β ≥ ε to keep the Beta well-defined.
    """
    q_c = min(1.0 - 1e-6, max(1e-6, float(q)))
    n = max(2.0, float(n_eff))
    alpha = max(1e-3, q_c * n)
    beta = max(1e-3, (1.0 - q_c) * n)
    return alpha, beta


def conviction_yes(q: float, p: float, n_eff: float) -> BayesianConviction:
    """P(true > p) under Beta prior centered at q."""
    alpha, b = beta_params(q, n_eff)
    # 1 - F(p) = survival function
    conv = float(1.0 - beta_dist.cdf(p, alpha, b))
    return BayesianConviction(
        alpha=alpha, beta=b, n_eff=n_eff, conviction=conv, side="YES"
    )


def conviction_no(q: float, p: float, n_eff: float) -> BayesianConviction:
    """P(true < p) under Beta prior centered at q (NO / DOWN side)."""
    alpha, b = beta_params(q, n_eff)
    conv = float(beta_dist.cdf(p, alpha, b))
    return BayesianConviction(
        alpha=alpha, beta=b, n_eff=n_eff, conviction=conv, side="NO"
    )


def bayesian_conviction(
    q: float,
    p: float,
    n_eff: float,
    *,
    side: str,
) -> BayesianConviction:
    """Dispatch YES/UP vs NO/DOWN conviction."""
    side_u = side.upper()
    if side_u in ("YES", "UP"):
        return conviction_yes(q, p, n_eff)
    return conviction_no(q, p, n_eff)


def passes_hard_entry_filter(
    q: float,
    p: float,
    conviction: float,
    *,
    min_edge: float = 0.06,
    min_conviction: float = 0.92,
    extreme_q_high: float = 0.78,
    extreme_q_low: float = 0.22,
    extreme_anchor: str = "q",
    live_real_q: bool = False,
    extreme_p_high: float | None = None,
    extreme_p_low: float | None = None,
    net_edge: float | None = None,
    calibrated_q: bool = False,
) -> tuple[bool, list[str]]:
    """Hard entry filter.

    edge >= min_edge  (edge = net_edge after costs when provided, else |q-p|)
    AND conviction >= min_conviction
    AND extreme stretch:

    - Default / synthetic (``extreme_anchor=q``): model q must be extreme.
      This is what delivers ~90% WR on the synthetic suite.
    - Live real-q (``live_real_q=True`` and q is mid): Polymarket p must be
      stretched instead. Live CEX ``cex_implied_up`` sits ~0.4–0.6 and never
      clears q≥0.85 — without this branch the desk takes zero trades.
    """
    reasons: list[str] = []
    # Compare the NET edge (after costs) to min_edge when the caller supplies
    # it; fall back to gross |q-p| otherwise. Costs must not be free.
    gate_edge = abs(q - p) if net_edge is None else float(net_edge)
    if gate_edge < min_edge:
        label = "edge" if net_edge is None else "net_edge"
        reasons.append(f"{label}={gate_edge:.4f}<{min_edge}")
    if conviction < min_conviction:
        reasons.append(f"conviction={conviction:.4f}<{min_conviction}")

    q_ext = q >= extreme_q_high or q <= extreme_q_low
    p_hi = float(extreme_p_high if extreme_p_high is not None else extreme_q_high)
    p_lo = float(extreme_p_low if extreme_p_low is not None else extreme_q_low)
    p_ext = p >= p_hi or p <= p_lo
    anchor = (extreme_anchor or "q").strip().lower()

    # Calibrated q (barrier price / null control): gap |q-p| is the signal.
    # Still block mild-model fades of crowded PM into lottery tickets
    # (live paper: side <=0.25 ~5% WR, large negative PnL).
    if calibrated_q:
        side_is_no = q < p  # model below market → fade UP / buy NO
        side_px = (1.0 - p) if side_is_no else p
        q_lean = abs(float(q) - 0.5)
        if side_px <= 0.25:
            reasons.append(
                f"cheap_fade_blocked: side_px={side_px:.3f}<=0.25 q={q:.3f} p={p:.3f}"
            )
        elif (p >= 0.80 or p <= 0.20) and q_lean < 0.12:
            reasons.append(
                f"mid_q_fade: |q-0.5|={q_lean:.3f}<0.12 cannot fade p={p:.3f}"
            )
        return (len(reasons) == 0), reasons

    # Live real-q: mid CEX q → require stretched Polymarket p (fade path).
    if live_real_q and not q_ext:
        extreme_ok = p_ext
        if not extreme_ok:
            reasons.append(
                f"live_real_q: p={p:.3f} not stretched "
                f"(need ≥{p_hi} or ≤{p_lo}; q={q:.3f} mid)"
            )
        else:
            q_lean = abs(float(q) - 0.5)
            side_is_no = q < p
            side_px = (1.0 - p) if side_is_no else p
            if side_px <= 0.25:
                reasons.append(
                    f"cheap_fade_blocked: side_px={side_px:.3f}<=0.25"
                )
            elif q_lean < 0.12:
                reasons.append(
                    f"mid_q_fade: |q-0.5|={q_lean:.3f}<0.12 cannot fade p={p:.3f}"
                )
            elif (p >= 0.80 or p <= 0.20) and abs(q - p) < 0.18:
                reasons.append(
                    f"extreme_p_fade_edge={abs(q-p):.4f}<0.18 (p={p:.3f} q={q:.3f})"
                )
        return (len(reasons) == 0), reasons

    if anchor == "none":
        extreme_ok = True
    elif anchor == "p":
        extreme_ok = p_ext
    elif anchor == "either":
        extreme_ok = q_ext or p_ext
    else:
        extreme_ok = q_ext

    if not extreme_ok:
        if anchor == "p":
            reasons.append(
                f"p={p:.3f} not extreme (need ≥{p_hi} or ≤{p_lo})"
            )
        elif anchor == "either":
            reasons.append(
                f"q={q:.3f}/p={p:.3f} not extreme "
                f"(need q≥{extreme_q_high}/≤{extreme_q_low} or p≥{p_hi}/≤{p_lo})"
            )
        else:
            reasons.append(
                f"q={q:.3f} not extreme (need ≥{extreme_q_high} or ≤{extreme_q_low})"
            )
    return (len(reasons) == 0), reasons
