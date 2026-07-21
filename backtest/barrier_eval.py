"""Barrier-price evaluation against real outcomes — the money question.

Given the paper fleet's real trades, this measures whether pricing the
up/down contract as a log-normal barrier (P(close > window-open | fresh CEX
spot, time-left, realized vol)) actually beats the market on real outcomes
after costs — and whether our volatility estimate is right, which is the
make-or-break input.

Everything is INJECTABLE (open_price_fn, window_path_fn) so it unit-tests
offline with synthetic prices; in production it uses the real CEX fetchers
(price_at_timestamp / klines). Outcomes are RECOMPUTED from open vs close,
independent of the possibly-buggy stored `won`.

Honest by construction: Wilson intervals, an INSUFFICIENT-N guard, and an
explicit note that the τ (time-in-window at entry) is an approximation the
ledger can't pin down exactly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from strategy.advanced_signals import (
    DEFAULT_SIGMA_ANN,
    SEC_PER_YEAR,
    barrier_implied_up,
    realized_sigma_ann,
)

logger = logging.getLogger(__name__)

WINDOW_SEC = {"5m": 300, "15m": 900}
MIN_TRADES_FOR_EDGE = 100

try:
    from scipy.stats import norm as _norm

    def _ppf(p: float) -> float:
        return float(_norm.ppf(min(1 - 1e-9, max(1e-9, p))))
except Exception:  # pragma: no cover
    def _ppf(p: float) -> float:
        # Acklam approximation fallback (not used when scipy present)
        return 0.0


OpenPriceFn = Callable[[str, int], float]
WindowPathFn = Callable[[str, int, int], Sequence[tuple]]


@dataclass
class BarrierEvalConfig:
    entry_frac: float = 0.6      # assumed fraction of window elapsed at entry
    cost_bps: float = 150.0      # round-trip cost haircut for the gap-trade sim
    gap_min: float = 0.05        # only "trade" when |barrier_q - market_p| exceeds this
    sigma_floor: float = 0.40
    sigma_ceil: float = 2.5


def _clip(x: float) -> float:
    return min(1 - 1e-6, max(1e-6, x))


def _brier(qs, ys) -> float:
    return sum((q - y) ** 2 for q, y in zip(qs, ys)) / len(ys)


def _logloss(qs, ys) -> float:
    return -sum(y * math.log(_clip(q)) + (1 - y) * math.log(1 - _clip(q))
                for q, y in zip(qs, ys)) / len(ys)


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _implied_sigma_ann(market_p_up: float, spot: float, strike: float, tau_sec: float) -> Optional[float]:
    """Invert the barrier at the market price (shared impl in advanced_signals)."""
    from strategy.advanced_signals import implied_sigma_ann

    return implied_sigma_ann(market_p_up, spot, strike, tau_sec)


@dataclass
class BarrierEvalReport:
    n_evaluated: int = 0
    n_excluded: int = 0
    market_brier: Optional[float] = None
    barrier_brier: Optional[float] = None
    market_logloss: Optional[float] = None
    barrier_logloss: Optional[float] = None
    sigma_realized_median: Optional[float] = None
    sigma_implied_median: Optional[float] = None
    # Gap-trade sim (barrier disagrees with market beyond gap_min)
    n_gap_trades: int = 0
    gap_wins: int = 0
    gap_wr: float = 0.0
    gap_wilson_lo: float = 0.0
    gap_wilson_hi: float = 1.0
    gap_pnl_gross: float = 0.0
    gap_pnl_net: float = 0.0
    gap_profit_factor: float = 0.0
    sufficient_n: bool = False
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = ["=== BARRIER vs MARKET — real outcomes, after costs ==="]
        if not self.sufficient_n:
            lines += [
                f"*** INSUFFICIENT: {self.n_evaluated} evaluable trades "
                f"(need >= {MIN_TRADES_FOR_EDGE}). Descriptive only. ***",
            ]
        lines += [
            "",
            f"Evaluated:        {self.n_evaluated}  (excluded {self.n_excluded} — no cex/open ref)",
        ]
        if self.market_brier is not None:
            better = "BARRIER" if self.barrier_brier < self.market_brier else "MARKET"
            lines += [
                f"Brier  market={self.market_brier:.4f}  barrier={self.barrier_brier:.4f}  → {better} better",
                f"LogLoss market={self.market_logloss:.4f}  barrier={self.barrier_logloss:.4f}",
            ]
        if self.sigma_realized_median is not None:
            lines.append(
                f"σ_ann  realized(median)={self.sigma_realized_median:.2f}  "
                f"market-implied(median)={self.sigma_implied_median if self.sigma_implied_median is not None else float('nan'):.2f}"
            )
            lines.append(
                "  → if market-implied σ >> realized σ, the barrier will show "
                "systematic edge that is only REAL if our σ is right."
            )
        lines += [
            "",
            f"Gap-trade sim (|barrier−market| ≥ {0.05:.2f}, cost applied):",
            f"  trades={self.n_gap_trades}  WR={self.gap_wr:.1%} "
            f"(95% CI {self.gap_wilson_lo:.1%}–{self.gap_wilson_hi:.1%})",
            f"  PnL/unit gross={self.gap_pnl_gross:+.3f}  net={self.gap_pnl_net:+.3f}  "
            f"PF={self.gap_profit_factor:.2f}",
        ]
        for n in self.notes:
            lines.append(f"NOTE: {n}")
        return "\n".join(lines)


def _default_open_price_fn(asset: str, window_ts: int) -> float:
    try:
        from connectors.cex_realtime import price_at_timestamp

        return float(price_at_timestamp(asset, int(window_ts)) or 0.0)
    except Exception:  # pragma: no cover
        return 0.0


WINDOW_SEC_BY_TF = {"5m": 300, "15m": 900}


def evaluate_barrier(
    trades: Sequence,
    *,
    open_price_fn: OpenPriceFn = _default_open_price_fn,
    window_path_fn: Optional[WindowPathFn] = None,
    close_price_fn: Optional[OpenPriceFn] = None,
    exclude_equal_close: bool = False,
    cfg: Optional[BarrierEvalConfig] = None,
) -> BarrierEvalReport:
    cfg = cfg or BarrierEvalConfig()
    r = BarrierEvalReport()
    b_qs: list[float] = []
    m_qs: list[float] = []
    ys: list[float] = []
    sig_real: list[float] = []
    sig_impl: list[float] = []
    gap_pnls_gross: list[float] = []
    gap_pnls_net: list[float] = []

    for t in trades:
        spot = t.entry_cex
        window_sec = WINDOW_SEC.get(t.timeframe, 300)
        strike = float(open_price_fn(t.asset.upper(), int(t.window_ts)) or 0.0)
        # Close (outcome) reference: the Chainlink stream close when provided
        # (A1/A3 — the actual resolution price), else the logged exit.
        if close_price_fn is not None:
            close = float(close_price_fn(t.asset.upper(), int(t.window_ts) + window_sec) or 0.0)
        else:
            close = t.exit_cex
        if not spot or not close or spot <= 0 or close <= 0 or strike <= 0:
            r.n_excluded += 1
            continue
        # Coarse-oracle guard: close == strike means open and close landed in
        # the SAME on-chain round (no update inside the window) → the outcome
        # is indeterminate, not a real tie. Exclude rather than mis-score.
        if exclude_equal_close and abs(close - strike) < 1e-9:
            r.n_excluded += 1
            continue
        tau = max(1.0, window_sec * (1.0 - cfg.entry_frac))

        # Realized σ over the window (from klines) or prior fallback.
        sigma = None
        if window_path_fn is not None:
            path = window_path_fn(t.asset.upper(), int(t.window_ts), int(t.window_ts) + window_sec)
            prices = [float(p) for _, p in path] if path else []
            if len(prices) >= 6:
                # klines are ~60s apart
                sigma = realized_sigma_ann(prices, sample_sec=60.0,
                                           floor=cfg.sigma_floor, ceil=cfg.sigma_ceil)
        if sigma is None:
            sigma = DEFAULT_SIGMA_ANN
        sig_real.append(sigma)

        # Recompute the TRUE outcome from open vs close (correct reference).
        true_up = 1.0 if close > strike else 0.0
        market_p_up = float(t.p_up)
        barrier_q = barrier_implied_up(spot, strike, sigma, tau)

        b_qs.append(barrier_q)
        m_qs.append(market_p_up)
        ys.append(true_up)

        imp = _implied_sigma_ann(market_p_up, spot, strike, tau)
        if imp is not None and 0.05 < imp < 5.0:
            sig_impl.append(imp)

        # Gap-trade sim: take the barrier's side only when it disagrees enough.
        if abs(barrier_q - market_p_up) >= cfg.gap_min:
            side_up = barrier_q > market_p_up
            price = market_p_up if side_up else (1.0 - market_p_up)
            price = min(0.99, max(0.01, price))
            won = (side_up and true_up > 0.5) or ((not side_up) and true_up < 0.5)
            pnl_gross = (1.0 / price - 1.0) if won else -1.0
            cost = (cfg.cost_bps / 10_000.0) / price  # entry slippage on shares
            gap_pnls_gross.append(pnl_gross)
            gap_pnls_net.append(pnl_gross - cost)

    r.n_evaluated = len(ys)
    if r.n_evaluated == 0:
        r.notes.append("no evaluable trades (need entry_cex/exit_cex + open ref)")
        return r

    r.market_brier = _brier(m_qs, ys)
    r.barrier_brier = _brier(b_qs, ys)
    r.market_logloss = _logloss(m_qs, ys)
    r.barrier_logloss = _logloss(b_qs, ys)
    if sig_real:
        sig_real.sort()
        r.sigma_realized_median = sig_real[len(sig_real) // 2]
    if sig_impl:
        sig_impl.sort()
        r.sigma_implied_median = sig_impl[len(sig_impl) // 2]

    r.n_gap_trades = len(gap_pnls_gross)
    if r.n_gap_trades:
        r.gap_wins = sum(1 for p in gap_pnls_gross if p > 0)
        r.gap_wr = r.gap_wins / r.n_gap_trades
        r.gap_wilson_lo, r.gap_wilson_hi = _wilson(r.gap_wins, r.n_gap_trades)
        r.gap_pnl_gross = sum(gap_pnls_gross)
        r.gap_pnl_net = sum(gap_pnls_net)
        wins = sum(p for p in gap_pnls_net if p > 0)
        losses = sum(-p for p in gap_pnls_net if p < 0)
        r.gap_profit_factor = (wins / losses) if losses > 1e-9 else (99.0 if wins > 0 else 0.0)

    r.sufficient_n = r.n_evaluated >= MIN_TRADES_FOR_EDGE
    r.notes.append(
        f"τ approximated as window×(1−{cfg.entry_frac:.2f}); ledger lacks exact "
        "entry time — barrier magnitude is sensitive to this."
    )
    if not r.sufficient_n:
        r.notes.append(
            f"{r.n_evaluated} < {MIN_TRADES_FOR_EDGE}: no go/no-go call is honest yet"
        )
    if r.sigma_realized_median and r.sigma_implied_median:
        ratio = r.sigma_implied_median / max(1e-6, r.sigma_realized_median)
        if ratio > 1.3 or ratio < 0.77:
            r.notes.append(
                f"σ mismatch: market implies {ratio:.2f}× our realized σ — "
                "barrier edge is conditional on OUR σ being the correct one."
            )
    return r
