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
) -> tuple[bool, list[str]]:
    """Hard entry filter (exact product requirement).

    abs(q - p) >= min_edge
    AND conviction >= min_conviction
    AND (q >= extreme_q_high OR q <= extreme_q_low)
    """
    reasons: list[str] = []
    edge = abs(q - p)
    if edge < min_edge:
        reasons.append(f"edge={edge:.4f}<{min_edge}")
    if conviction < min_conviction:
        reasons.append(f"conviction={conviction:.4f}<{min_conviction}")
    if not (q >= extreme_q_high or q <= extreme_q_low):
        reasons.append(
            f"q={q:.3f} not extreme (need ≥{extreme_q_high} or ≤{extreme_q_low})"
        )
    return (len(reasons) == 0), reasons
