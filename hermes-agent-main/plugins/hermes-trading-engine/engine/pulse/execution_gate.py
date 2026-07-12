"""Strict Polymarket execution-quality gate for the BTC pulse (PAPER ONLY).

Hermes must NOT trade from signal probability alone. Before every paper trade, this gate
re-checks the candidate against orderbook REALITY — spread, depth, VWAP/estimated fill price,
slippage, tick size, min order size, time-to-resolution, liquidity cap, and partial-fill risk
— computing EV from the **VWAP fill over the live ask ladder, never the midpoint**. A trade is
accepted only if the after-slippage EV survives; every rejection carries an explicit reason.

This is an execution-realism gate; it can only BLOCK paper trades, never create or size one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# explicit, stable rejection reasons (acceptance criterion #3)
WIDE_SPREAD = "wide_spread"
INSUFFICIENT_DEPTH = "insufficient_depth"
NEGATIVE_EV = "negative_ev_after_slippage"
TOO_CLOSE = "too_close_to_resolution"
MIN_SIZE_OR_TICK = "min_size_or_tick_violation"
PARTIAL_FILL_RISK = "partial_fill_risk"
MISSING_MARKET_DATA = "missing_market_data"
STALE_ORDERBOOK = "stale_orderbook"
UNDERDOG_PRICE = "underdog_price_below_floor"
REASONS = (WIDE_SPREAD, INSUFFICIENT_DEPTH, NEGATIVE_EV, TOO_CLOSE, MIN_SIZE_OR_TICK,
           PARTIAL_FILL_RISK, MISSING_MARKET_DATA, STALE_ORDERBOOK, UNDERDOG_PRICE)


@dataclass
class ExecResult:
    accepted: bool
    reason: str                       # "accepted" or one of REASONS
    fill_price: Optional[float] = None   # VWAP fill (paper) — what we'd actually pay
    best_ask: Optional[float] = None
    vwap: Optional[float] = None
    slippage: float = 0.0             # vwap - best_ask
    ev_after_slippage: Optional[float] = None
    ev_at_mid: Optional[float] = None
    fillable_usd: float = 0.0
    spread: Optional[float] = None
    fee_rate: float = 0.0
    fee_per_share: float = 0.0
    fee_usd: float = 0.0

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "reason": self.reason,
                "fill_price": (round(self.fill_price, 6) if self.fill_price is not None else None),
                "best_ask": self.best_ask, "vwap": (round(self.vwap, 6) if self.vwap else None),
                "slippage": round(self.slippage, 6),
                "ev_after_slippage": (round(self.ev_after_slippage, 6)
                                      if self.ev_after_slippage is not None else None),
                "ev_at_mid": (round(self.ev_at_mid, 6) if self.ev_at_mid is not None else None),
                "fillable_usd": round(self.fillable_usd, 2), "spread": self.spread,
                "fee_rate": round(self.fee_rate, 6),
                "fee_per_share": round(self.fee_per_share, 6),
                "fee_usd": round(self.fee_usd, 6)}


def vwap_sell_bids(bids: list, shares: float) -> "tuple[Optional[float], float, float, bool]":
    """Walk the bid ladder (best->worst) selling up to ``shares``. Returns
    (vwap, proceeds_usd, shares_sold, fully_sold)."""
    sold = 0.0
    proceeds = 0.0
    for price, sz in (bids or []):
        if price <= 0 or sz <= 0:
            continue
        if sold >= float(shares) - 1e-9:
            break
        take = min(float(sz), float(shares) - sold)
        proceeds += take * price
        sold += take
    fully = sold >= float(shares) - 1e-9
    vwap = (proceeds / sold) if sold > 0 else None
    return vwap, proceeds, sold, fully


def vwap_fill(asks: list, size_usd: float) -> "tuple[Optional[float], float, float, bool]":
    """Walk the ask ladder (best->worst) spending up to ``size_usd``. Returns
    (vwap, filled_usd, filled_shares, fully_filled)."""
    spent = 0.0
    shares = 0.0
    for price, sz in (asks or []):
        if price <= 0 or sz <= 0:
            continue
        remaining = size_usd - spent
        if remaining <= 1e-9:
            break
        level_notional = price * sz
        take_notional = min(level_notional, remaining)
        spent += take_notional
        shares += take_notional / price
    fully = spent >= size_usd - 1e-9
    vwap = (spent / shares) if shares > 0 else None
    return vwap, spent, shares, fully


def _on_tick(price: float, tick: float) -> bool:
    if not tick or tick <= 0:
        return True
    units = price / tick
    return abs(units - round(units)) < 1e-6


def evaluate_execution(*, side: str, book, outcome_prob: float, size_usd: float,
                       tick_size: float, ttc_s: float,
                       min_seconds_to_close: float = 4.0, max_spread: float = 0.06,
                       min_depth_usd: float = 1.0, min_order_usd: float = 1.0,
                       max_depth_consume_frac: float = 0.5,
                       min_ev_after_slippage: float = 0.0,
                       min_fill_price: float = 0.0,
                       taker_fee_rate: float = 0.0,
                       now: Optional[float] = None,
                       max_book_age_s: float = 30.0) -> ExecResult:
    """Evaluate a candidate against orderbook reality. ``outcome_prob`` is the model
    probability of the outcome whose token we'd buy (so EV = outcome_prob - fill_price).

    ``min_fill_price`` rejects buying the UNDERDOG side (VWAP fill below the floor, e.g. 0.50): in
    a near-efficient 5-min market the price IS the probability, and the bot's model systematically
    overestimates cheap/tail sides (adverse selection) — live data showed underdog buys at ~28% win
    for the entire net loss, while favourites (>0.5) were net-positive. Proven edge sources (e.g. the
    graded CEX-lead) pass ``min_fill_price=0`` since their edge is exactly buying mispriced sides."""
    best_ask = book.best_ask if book else None
    spread = book.spread if book else None
    ask_depth = float(book.ask_depth_usd if book else 0.0)
    asks = book.asks if book else []
    mid = book.mid if book else None
    mid_fee = (max(0.0, float(taker_fee_rate)) * mid * (1.0 - mid)) if mid is not None else 0.0
    ev_at_mid = (outcome_prob - mid - mid_fee) if mid is not None else None

    def rej(reason, **kw):
        return ExecResult(False, reason, best_ask=best_ask, spread=spread,
                          ev_at_mid=ev_at_mid, **kw)

    # 0) market data present at all
    if book is None or best_ask is None or not asks:
        return rej(MISSING_MARKET_DATA)
    # 0b) stale orderbook — the book snapshot is older than max_book_age_s (only checked when a
    # real book timestamp + ``now`` are available; synthetic books with ts=0 skip this).
    if now is not None and max_book_age_s > 0 and getattr(book, "ts", 0):
        if (now - float(book.ts)) > max_book_age_s:
            return rej(STALE_ORDERBOOK)
    # 1) time-to-resolution
    if ttc_s <= min_seconds_to_close:
        return rej(TOO_CLOSE)
    # 2) min order size + tick validity
    if size_usd < min_order_usd:
        return rej(MIN_SIZE_OR_TICK)
    if not _on_tick(best_ask, tick_size):
        return rej(MIN_SIZE_OR_TICK)
    # 3) spread
    if spread is None or spread > max_spread:
        return rej(WIDE_SPREAD)
    # 4) absolute depth floor
    if ask_depth < min_depth_usd:
        return rej(INSUFFICIENT_DEPTH)
    # 5) partial-fill risk: must fully fill the size within the ladder AND not eat too much depth
    vwap, filled_usd, shares, fully = vwap_fill(asks, size_usd)
    if not fully or vwap is None:
        return rej(PARTIAL_FILL_RISK, fillable_usd=filled_usd, vwap=vwap)
    if ask_depth > 0 and (size_usd / ask_depth) > max_depth_consume_frac:
        return rej(PARTIAL_FILL_RISK, fillable_usd=filled_usd, vwap=vwap)
    # 5b) underdog-price floor: do not BUY a side whose VWAP fill is below the floor (betting the
    # less-likely outcome). The market price is the best probability estimate; the bot's opinion has
    # negative edge on cheap/tail sides. Skipped (floor=0) for proven edge sources.
    if min_fill_price > 0 and vwap < min_fill_price:
        return ExecResult(False, UNDERDOG_PRICE, fill_price=None, best_ask=best_ask, vwap=vwap,
                          slippage=(vwap - best_ask), ev_at_mid=ev_at_mid,
                          fillable_usd=filled_usd, spread=spread)
    # 6) EV after VWAP/slippage (NOT midpoint)
    slippage = vwap - best_ask
    fee_rate = max(0.0, float(taker_fee_rate))
    fee_per_share = fee_rate * vwap * (1.0 - vwap)
    fee_usd = shares * fee_per_share
    ev = outcome_prob - vwap - fee_per_share
    if ev <= min_ev_after_slippage:
        return ExecResult(False, NEGATIVE_EV, fill_price=None, best_ask=best_ask, vwap=vwap,
                          slippage=slippage, ev_after_slippage=ev, ev_at_mid=ev_at_mid,
                           fillable_usd=filled_usd, spread=spread, fee_rate=fee_rate,
                           fee_per_share=fee_per_share, fee_usd=fee_usd)
    return ExecResult(True, "accepted", fill_price=vwap, best_ask=best_ask, vwap=vwap,
                      slippage=slippage, ev_after_slippage=ev, ev_at_mid=ev_at_mid,
                      fillable_usd=filled_usd, spread=spread, fee_rate=fee_rate,
                      fee_per_share=fee_per_share, fee_usd=fee_usd)
