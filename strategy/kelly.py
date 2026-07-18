"""Polymarket Kelly Criterion — exact formulations.

Yes bet:  f_star = (q - p) / (1 - p)
No bet:   f_star = (p - q) / p
f = kappa * min(f_star, 1.0)

where p = market price of YES, q = model P(YES).
Position cost = f * bankroll, capped at max_single_market_pct.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KellyResult:
    """Kelly fraction for a binary Polymarket contract."""

    side: str  # "YES" | "NO"
    f_star: float
    f: float
    kappa: float
    size_usd: float
    capped: bool


def kelly_yes(q: float, p: float) -> float:
    """Full-Kelly fraction for a YES bet.

    f* = (q - p) / (1 - p)

    Pays $1 if YES resolves; costs p per share. Edge exists when q > p.
    """
    if p >= 1.0 - 1e-12:
        return 0.0
    return (q - p) / (1.0 - p)


def kelly_no(q: float, p: float) -> float:
    """Full-Kelly fraction for a NO bet.

    f* = (p - q) / p

    Equivalent to betting against YES when q < p. Cost of NO is (1-p) in
    cash terms on Polymarket; the Kelly form above is the standard
    binary-odds restatement in terms of YES price p.
    """
    if p <= 1e-12:
        return 0.0
    return (p - q) / p


def expected_cost_frac(
    p_side: float,
    q_side: float,
    *,
    slippage_bps: float,
    fee_bps: float,
) -> float:
    """Expected per-share transaction cost, in probability/price units.

    A hold-to-resolution binary pays $1 if the chosen side wins. Costs:
      - entry slippage: pay ``p_side * slip`` extra crossing the book;
      - settlement fee: on a win (prob ~ q_side) the $1 payout is docked
        ``fee``, so expected fee cost ≈ ``q_side * fee``.
    Returns a value directly comparable to the gross edge (q_side - p_side).
    """
    slip = max(0.0, float(slippage_bps)) / 10_000.0
    fee = max(0.0, float(fee_bps)) / 10_000.0
    return float(max(0.0, p_side)) * slip + float(max(0.0, q_side)) * fee


def net_edge_after_costs(
    p_side: float,
    q_side: float,
    *,
    slippage_bps: float,
    fee_bps: float,
) -> float:
    """Gross edge (q_side - p_side) minus expected slippage + fees."""
    gross = float(q_side) - float(p_side)
    return gross - expected_cost_frac(
        p_side, q_side, slippage_bps=slippage_bps, fee_bps=fee_bps
    )


def apply_kappa(f_star: float, kappa: float) -> float:
    """Fractional Kelly: f = kappa * min(f_star, 1.0). Negative → 0."""
    if f_star <= 0:
        return 0.0
    return float(kappa) * min(float(f_star), 1.0)


def kelly_size(
    *,
    q: float,
    p: float,
    side: str,
    bankroll: float,
    kappa: float,
    max_pct: float = 0.10,
) -> KellyResult:
    """Compute capped position cost for YES or NO.

    Parameters
    ----------
    q : model P(YES / UP)
    p : market YES price
    side : "YES"/"UP" or "NO"/"DOWN"
    bankroll : current cash equity
    kappa : fractional Kelly multiplier (base 0.35; guards may lower)
    max_pct : hard cap (never exceed this fraction of bankroll)
    """
    side_u = side.upper()
    if side_u in ("YES", "UP"):
        f_star = kelly_yes(q, p)
        side_label = "YES" if side_u == "YES" else "UP"
    else:
        f_star = kelly_no(q, p)
        side_label = "NO" if side_u == "NO" else "DOWN"

    f = apply_kappa(f_star, kappa)
    raw = f * bankroll
    cap = max_pct * bankroll
    capped = raw > cap + 1e-9
    size = min(raw, cap) if raw > 0 else 0.0
    return KellyResult(
        side=side_label,
        f_star=float(f_star),
        f=float(f),
        kappa=float(kappa),
        size_usd=round(size, 4),
        capped=capped,
    )
