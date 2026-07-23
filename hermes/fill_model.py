"""C2 — conservative paper-fill model: paper must be a PESSIMISTIC bound.

Paper fills cannot model adverse selection: the counterparty who fills you
near the strike, late in the window, knows something — live fills will be
worse than any replay of the visible book. Since that cannot be simulated,
paper is haircut instead:

  1. DEPTH: fill at most PARTICIPATION_MAX (25%) of the visible ask notional
     within MAX_IMPACT_CENTS (1¢) of the best quote — visible depth is not
     your depth (same constants as the C1 capacity ceiling).
  2. PRICE: extra slippage that grows as the contract trades near 0.5 —
     spot≈strike is where informed flow concentrates, and a mid of ~0.5 IS
     the observable signature of a near-the-money window.

Every paper report must carry PAPER_CAVEAT (backtest/paper_ledger wires it);
paper results without it overstate the strategy by construction.
"""

from __future__ import annotations

from typing import Optional, Sequence

from backtest.capacity import MAX_IMPACT_CENTS, PARTICIPATION_MAX, fillable_usd

# Extra slippage at mid = 0.5 (near-the-money, adverse-selection proxy),
# fading linearly to zero once |mid − 0.5| ≥ NEAR_MONEY_BAND.
NEAR_MONEY_EXTRA_BPS = 150.0
NEAR_MONEY_BAND = 0.15

PAPER_CAVEAT = (
    "PAPER CEILING: fills are haircut (≤25% of visible depth within 1¢, extra "
    "slippage near the money) but CANNOT model adverse selection — the "
    "counterparty who fills you near the strike knows something. Treat every "
    "number above as an OPTIMISTIC bound on live performance even after the "
    "haircuts; never quote paper results without this caveat."
)


def near_money_penalty_bps(mid: Optional[float]) -> float:
    """Extra slippage (bps of price) as the contract approaches 50/50."""
    if mid is None or not (0.0 < mid < 1.0):
        return 0.0
    dist = abs(float(mid) - 0.5)
    if dist >= NEAR_MONEY_BAND:
        return 0.0
    return NEAR_MONEY_EXTRA_BPS * (1.0 - dist / NEAR_MONEY_BAND)


def conservative_paper_fill(
    asks: Sequence[tuple[float, float]],
    size_usd: float,
    limit_price: float,
    *,
    mid: Optional[float] = None,
    maker: bool = False,
) -> tuple[float, float, float, str]:
    """Pessimistic paper BUY against ``[(price, size_shares), ...]`` best-first.

    Returns (filled_usd, fill_price, slippage_bps, note):
      - filled_usd capped at the participation share of near-touch depth;
      - taker (default): fill_price = VWAP over the filled notional;
      - maker=True: models a resting order near the mid — price = mid + 25%
        of the half-spread (you rarely capture the whole half-spread: queue
        position, quote fades). HONESTY LIMIT: non-fills are NOT modeled, so
        maker paper still overstates fill frequency; the near-money penalty
        stays FULL because makers near 0.5 are filled exactly when informed
        flow arrives (adverse selection is worse for makers, not better).
      - both worsened by the near-the-money penalty; taker never better than
        the limit; slippage_bps measured vs mid when known.
    """
    size_usd = max(0.0, float(size_usd))
    if not asks or size_usd <= 0:
        # No book → keep the caller's fallback path (legacy fixed-slip fill).
        return size_usd, 0.0, 0.0, "no_book"

    cap = fillable_usd(
        asks, max_impact_cents=MAX_IMPACT_CENTS, participation=PARTICIPATION_MAX
    )
    filled = min(size_usd, cap)
    note = ""
    if filled < size_usd:
        note = f"depth_cap ${size_usd:.0f}→${filled:.0f} (25% of near-touch)"
    if filled <= 0:
        return 0.0, float(asks[0][0]), 0.0, note or "no_depth"

    if maker and mid is not None and 0.0 < mid < 1.0:
        # Resting near the mid: capture ~75% of the half-spread, never more.
        best = float(asks[0][0])
        base = mid + 0.25 * max(0.0, best - mid)
        note = (note + "; " if note else "") + "maker_mid_fill"
    else:
        # Taker VWAP walk for the filled notional.
        remaining = filled
        cost = 0.0
        shares = 0.0
        for price, size in asks:
            price, size = float(price), float(size)
            if price <= 0 or size <= 0:
                continue
            take = min(remaining, price * size)
            cost += take
            shares += take / price
            remaining -= take
            if remaining <= 1e-9:
                break
        base = cost / shares if shares > 0 else float(asks[0][0])

    penalty = near_money_penalty_bps(mid if mid is not None else base)
    px = base * (1.0 + penalty / 10_000.0)
    if maker and mid is not None and 0.0 < mid < 1.0:
        px = min(0.99, max(px, float(mid)))  # maker never fills below mid
    else:
        # Aggressor never fills better than their limit; clamp to book domain.
        px = min(0.99, max(px, float(limit_price)))
    ref = mid if (mid is not None and mid > 0) else vwap
    slip_bps = max(0.0, (px - ref) / ref * 10_000.0) if ref else 0.0
    if penalty > 0:
        note = (note + "; " if note else "") + f"near_money +{penalty:.0f}bps"
    return float(filled), float(px), float(slip_bps), note
