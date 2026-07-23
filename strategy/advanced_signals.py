"""Advanced multi-signal ensemble for Hermes Agent v3 (real CEX q).

Upstream context (read before editing):
  - hermes/mispricing.py — toy ``_cex_implied_up`` is the upgrade point
  - strategy/enhanced_misprice.py — consumes q; live_real_q + hard filters stay sacred
  - models/config.py — STRICT_REAL_FREEZE must never be loosened
  - connectors/cex_realtime.py — rolling price history for multi-TF slopes

Design goals
------------
* Replace toy momentum→q with a Hurst-gated fusion of:
  multi-TF slopes, order-book imbalance, log-normal cash-or-nothing,
  OU mean-reversion, Kalman latent fair-prob, GARCH vol.
* NEVER invent artificial extreme q (0.97 / 0.03). Clamp to [0.05, 0.95].
* Graceful fallback to simple momentum mapping when history/book is thin.
* Pure numpy/scipy only (no statsmodels dependency).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np
from scipy.stats import norm

logger = logging.getLogger(__name__)

SEC_PER_YEAR = 31_536_000.0  # crypto trades 24/7
# Cold-start annualized-vol prior. Short-horizon (5–15m) BTC realized vol is
# high — a market pricing a +0.2% move at ~0.79 implies σ≈1.1. Realized σ
# from live history dominates this; the prior only covers thin history.
# NOTE: σ calibration is the make-or-break for barrier edge and must be
# validated against real moves — flagged, not assumed.
DEFAULT_SIGMA_ANN = 1.00

# Default multi-TF windows (seconds) — longer windows preferred
DEFAULT_TF_WINDOWS = (30.0, 60.0, 120.0, 240.0)
DEFAULT_TF_WEIGHTS = (0.15, 0.20, 0.30, 0.35)

# Fusion: swarm vs market blend (tunable via config)
DEFAULT_SWARM_WEIGHT = 0.70
DEFAULT_MARKET_BLEND = 0.30


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class AdvancedSignalResult:
    """Ensemble output — drop-in replacement for toy cex_implied_up."""

    q: float
    conviction_boost: float = 0.0  # additive hint for mispricing layer [0, 0.15]
    regime: str = "unknown"  # mean_reversion | momentum | unknown
    components: dict[str, float] = field(default_factory=dict)
    features: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    used_fallback: bool = False
    reason: str = ""


def _clamp01(x: float, lo: float = 0.05, hi: float = 0.95) -> float:
    return float(max(lo, min(hi, x)))


def _as_float_array(xs: Sequence[float]) -> np.ndarray:
    return np.asarray([float(x) for x in xs], dtype=float)


# ---------------------------------------------------------------------------
# Multi-timeframe composite momentum
# ---------------------------------------------------------------------------

def slope_for_window(
    times: Sequence[float],
    prices: Sequence[float],
    window_sec: float,
) -> float:
    """OLS slope β1 of price vs time over the last ``window_sec``, scaled by mean price.

    Returns dimensionless slope ≈ fractional change per second * window,
    so values are comparable across windows. 0 if insufficient data.
    """
    t = _as_float_array(times)
    p = _as_float_array(prices)
    if t.size < 3 or p.size != t.size:
        return 0.0
    t1 = float(t[-1])
    mask = t >= (t1 - float(window_sec))
    if int(mask.sum()) < 3:
        return 0.0
    tt = t[mask]
    pp = p[mask]
    mean_p = float(np.mean(pp))
    if mean_p <= 0:
        return 0.0
    # Center time for numerical stability
    tc = tt - tt[0]
    # β1 from OLS: y = a + b x
    var_t = float(np.var(tc))
    if var_t < 1e-12:
        return 0.0
    cov = float(np.mean((tc - tc.mean()) * (pp - pp.mean())))
    beta1 = cov / var_t  # price units per second
    # Normalize: fractional move over the window
    return float((beta1 * window_sec) / mean_p)


def multi_tf_weighted_slope(
    times: Sequence[float],
    prices: Sequence[float],
    *,
    windows: Sequence[float] = DEFAULT_TF_WINDOWS,
    weights: Sequence[float] = DEFAULT_TF_WEIGHTS,
) -> tuple[float, dict[str, float]]:
    """Σ slope_tf * w_tf with adaptive renormalization of available windows."""
    slopes: dict[str, float] = {}
    w_ok: list[float] = []
    s_ok: list[float] = []
    for w, wt in zip(windows, weights):
        s = slope_for_window(times, prices, float(w))
        key = f"slope_{int(w)}s"
        slopes[key] = s
        # Only weight windows that produced a real slope (non-zero history)
        if abs(s) > 0 or len(times) >= 3:
            w_ok.append(float(wt))
            s_ok.append(s)
    if not w_ok or sum(w_ok) <= 0:
        return 0.0, slopes
    w_arr = np.asarray(w_ok, dtype=float)
    w_arr = w_arr / w_arr.sum()
    composite = float(np.dot(w_arr, np.asarray(s_ok, dtype=float)))
    slopes["weighted_slope"] = composite
    return composite, slopes


# ---------------------------------------------------------------------------
# Order book: OBI, IR, VAMP
# ---------------------------------------------------------------------------

def order_book_metrics(
    bids: Sequence[BookLevel | tuple[float, float]],
    asks: Sequence[BookLevel | tuple[float, float]],
    *,
    levels: int = 5,
) -> dict[str, float]:
    """Multi-level OBI, IR, VAMP from bid/ask ladders.

    OBI = (Q_bid - Q_ask) / (Q_bid + Q_ask)
    IR  = Bid_vol / (Bid_vol + Ask_vol)
    VAMP = (P_bid*Q_ask + P_ask*Q_bid) / (Q_bid + Q_ask)  (top-of-book style)
    """
    def _levels(side: Sequence[BookLevel | tuple[float, float]]) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for row in list(side)[: max(1, levels)]:
            if isinstance(row, BookLevel):
                out.append((float(row.price), float(row.size)))
            else:
                out.append((float(row[0]), float(row[1])))
        return out

    b = _levels(bids)
    a = _levels(asks)
    q_bid = sum(sz for _, sz in b) if b else 0.0
    q_ask = sum(sz for _, sz in a) if a else 0.0
    denom = q_bid + q_ask
    if denom <= 0:
        return {"obi": 0.0, "ir": 0.5, "vamp": 0.0, "q_bid": 0.0, "q_ask": 0.0}

    obi = (q_bid - q_ask) / denom
    ir = q_bid / denom
    p_bid = b[0][0] if b else 0.0
    p_ask = a[0][0] if a else 0.0
    if p_bid > 0 and p_ask > 0:
        vamp = (p_bid * q_ask + p_ask * q_bid) / denom
    else:
        vamp = p_bid or p_ask
    return {
        "obi": float(obi),
        "ir": float(ir),
        "vamp": float(vamp),
        "q_bid": float(q_bid),
        "q_ask": float(q_ask),
    }


def obi_to_prob(obi: float, *, scale: float = 0.20) -> float:
    """Map OBI ∈ [-1,1] → P(UP) around 0.5."""
    return _clamp01(0.5 + float(obi) * scale)


# ---------------------------------------------------------------------------
# GARCH(1,1) + log-normal CEX probability
# ---------------------------------------------------------------------------

def estimate_garch11(
    returns: Sequence[float],
    *,
    omega: float = 1e-6,
    alpha: float = 0.08,
    beta: float = 0.90,
) -> float:
    """One-step-ahead GARCH(1,1) volatility (std of returns).

    Uses fixed persistence parameters (α+β<1) with ω calibrated lightly to
    sample variance when the series is long enough. Pure numpy recursion.
    """
    r = _as_float_array(returns)
    if r.size < 5:
        return float(np.std(r)) if r.size >= 2 else 0.01
    # Keep stationarity
    alpha = float(min(0.3, max(0.01, alpha)))
    beta = float(min(0.95, max(0.5, beta)))
    if alpha + beta >= 0.999:
        beta = 0.999 - alpha
    sample_var = float(np.var(r))
    omega = max(1e-12, float(omega) if omega > 0 else sample_var * (1.0 - alpha - beta))
    # Unconditional var init
    var_t = sample_var if sample_var > 0 else omega / max(1e-9, 1.0 - alpha - beta)
    for rt in r:
        var_t = omega + alpha * float(rt) ** 2 + beta * var_t
    return float(math.sqrt(max(var_t, 1e-16)))


def realized_sigma_ann(
    prices: Sequence[float],
    *,
    sample_sec: float = 1.0,
    floor: float = 0.40,  # short-horizon crypto vol rarely below ~40% annualized
    ceil: float = 2.5,
) -> Optional[float]:
    """Annualized volatility from a price series (std of log returns).

    ``sample_sec`` is the spacing between samples so we can annualize. Returns
    None when history is too thin to estimate; caller falls back to a prior.
    """
    p = _as_float_array(prices)
    if p.size < 6:
        return None
    rets = np.diff(np.log(np.maximum(p, 1e-12)))
    if rets.size < 5:
        return None
    sd = float(np.std(rets))
    if sd <= 0:
        return None
    ann = sd * math.sqrt(SEC_PER_YEAR / max(1e-6, float(sample_sec)))
    return float(min(ceil, max(floor, ann)))


def garch_sigma_ann(
    prices: Sequence[float],
    *,
    sample_sec: float = 1.0,
    floor: float = 0.40,
    ceil: float = 2.5,
) -> Optional[float]:
    """Annualized GARCH(1,1) one-step vol from a price series."""
    p = _as_float_array(prices)
    if p.size < 10:
        return None
    rets = np.diff(p) / np.maximum(p[:-1], 1e-12)
    sig = estimate_garch11(rets)
    if sig <= 0:
        return None
    ann = sig * math.sqrt(SEC_PER_YEAR / max(1e-6, float(sample_sec)))
    return float(min(ceil, max(floor, ann)))


def implied_sigma_ann(
    p_up: float,
    spot: float,
    strike: float,
    seconds_to_resolution: float,
) -> Optional[float]:
    """Invert the barrier at a market price → the σ the market is pricing.

    p ≈ Φ(ln(S/K)/(σ√T)) → σ = ln(S/K) / (z·√T). None when unidentified
    (market at ~0.5 or spot at strike).
    """
    if spot <= 0 or strike <= 0 or seconds_to_resolution <= 0:
        return None
    p = min(1 - 1e-9, max(1e-9, float(p_up)))
    z = float(norm.ppf(p))
    if abs(z) < 1e-6:
        return None
    lr = math.log(spot / strike)
    if abs(lr) < 1e-9:
        return None
    T = float(seconds_to_resolution) / SEC_PER_YEAR
    sig = lr / (z * math.sqrt(T))
    return float(sig) if sig > 0 else None


def barrier_implied_up(
    spot: float,
    strike: float,
    sigma_ann: float,
    seconds_to_resolution: float,
) -> float:
    """Calibrated P(S_close > strike) — the price of the up/down contract.

    Log-normal barrier probability with ~zero real-world drift over the short
    horizon: q = Φ( (ln(S/K) − ½σ²T) / (σ√T) ). ``strike`` is the window-OPEN
    CEX price (the resolution reference). This is what the market itself
    prices; feeding a FRESH spot is where any latency edge over Polymarket
    comes from. Clamped to [0.05, 0.95].
    """
    S = float(spot)
    K = float(strike)
    if S <= 0 or K <= 0:
        return 0.5
    T = max(1.0, float(seconds_to_resolution)) / SEC_PER_YEAR
    sig = max(0.05, float(sigma_ann))
    denom = sig * math.sqrt(T)
    if denom <= 1e-12:
        if S > K:
            return 0.95
        if S < K:
            return 0.05
        return 0.5
    d = (math.log(S / K) - 0.5 * sig * sig * T) / denom
    return _clamp01(float(norm.cdf(d)))


# ── Drift-aware barrier (the anti-fade fix) ─────────────────────────────────
# Live evidence (last-10h report 2026-07-22): the DRIFTLESS barrier fades
# collapsed sides — 72 cheap tickets, 2.8% WR vs ~20% fair (−4.3σ). A side
# collapsing to 0.15 mid-window is INFORMATION (the move already happened);
# pricing the window with μ=0 calls that side "cheap" and buys it. Adding an
# intra-window drift estimate makes the model agree with the market in the
# tails and only disagree on genuine spot-freshness gaps.

DRIFT_LOOKBACK_SEC = 180.0
DRIFT_SHRINK = 0.5        # regress the raw estimate halfway to zero
# |μ| cap, annualized. Scale check: a 25bps move in 3 minutes annualizes to
# ~440; to matter at all, μ·T must be comparable to σ·√T ≈ 25bps over 5 min,
# which needs μ ≈ σ/√T ≈ 260. A single-digit clamp would make drift a no-op.
DRIFT_CLAMP_ANN = 500.0


def drift_mu_ann(
    prices: list[float],
    times: list[float],
    *,
    lookback_sec: float = DRIFT_LOOKBACK_SEC,
    shrink: float = DRIFT_SHRINK,
    clamp: float = DRIFT_CLAMP_ANN,
) -> float:
    """Annualized intra-window drift from the recent tick history, shrunk.

    Log-return over the trailing ``lookback_sec``, annualized, then shrunk
    toward zero (drift estimates at 3-minute scale are mostly noise — the
    shrink keeps the direction while damping the magnitude) and clamped.
    Returns 0.0 when history is too thin.
    """
    if not prices or not times or len(prices) != len(times) or len(prices) < 3:
        return 0.0
    t_end = float(times[-1])
    p_end = float(prices[-1])
    p_start = None
    t_start = None
    for t, p in zip(times, prices):
        if t_end - float(t) <= lookback_sec:
            p_start = float(p)
            t_start = float(t)
            break
    if p_start is None or p_start <= 0 or p_end <= 0 or t_start is None:
        return 0.0
    dt = max(1.0, t_end - t_start)
    mu = math.log(p_end / p_start) * (SEC_PER_YEAR / dt) * shrink
    return float(max(-clamp, min(clamp, mu)))


def standardized_distance(
    spot: float, strike: float, sigma_ann: float, tau_sec: float
) -> float:
    """|ln(S/K)| in units of σ√τ — how many remaining-vol standard deviations
    the price sits from the strike. d ≥ 2 ⇒ the window is ~settled (the
    fav_sniper entry condition); d ≈ 0 ⇒ a coin-flip regardless of price."""
    S, K = float(spot), float(strike)
    if S <= 0 or K <= 0:
        return 0.0
    T = max(1.0, float(tau_sec)) / SEC_PER_YEAR
    denom = max(0.05, float(sigma_ann)) * math.sqrt(T)
    if denom <= 1e-12:
        return 0.0
    return abs(math.log(S / K)) / denom


def barrier_implied_up_drift(
    spot: float,
    strike: float,
    sigma_ann: float,
    seconds_to_resolution: float,
    mu_ann: float,
) -> float:
    """P(S_close > strike) with drift: q = Φ((ln(S/K) + (μ−½σ²)T)/(σ√T))."""
    S = float(spot)
    K = float(strike)
    if S <= 0 or K <= 0:
        return 0.5
    T = max(1.0, float(seconds_to_resolution)) / SEC_PER_YEAR
    sig = max(0.05, float(sigma_ann))
    denom = sig * math.sqrt(T)
    if denom <= 1e-12:
        return 0.95 if S > K else (0.05 if S < K else 0.5)
    d = (math.log(S / K) + (float(mu_ann) - 0.5 * sig * sig) * T) / denom
    return _clamp01(float(norm.cdf(d)))


def lognormal_cex_prob(
    spot: float,
    strike: float,
    sigma: float,
    t_years: float,
) -> float:
    """Cash-or-nothing style Φ(ln(S/K) / (σ√T)) — P(S_T > K) under log-normal."""
    S = float(spot)
    K = float(strike)
    if S <= 0 or K <= 0:
        return 0.5
    sig = max(1e-6, float(sigma))
    T = max(1e-9, float(t_years))
    d = math.log(S / K) / (sig * math.sqrt(T))
    return _clamp01(float(norm.cdf(d)))


# ---------------------------------------------------------------------------
# Ornstein–Uhlenbeck via AR(1) + Hurst (R/S)
# ---------------------------------------------------------------------------

def estimate_ou_ar1(prices: Sequence[float], dt: float = 1.0) -> dict[str, float]:
    """Estimate OU params via AR(1): x_{t+1} = a + b x_t + ε.

    θ = -ln(b)/dt,  μ = a/(1-b),  σ from residual variance.
    """
    x = _as_float_array(prices)
    if x.size < 8:
        return {"theta": 0.0, "mu": float(np.mean(x)) if x.size else 0.0, "sigma": 0.0, "b": 1.0}
    x0 = x[:-1]
    x1 = x[1:]
    # OLS
    n = float(x0.size)
    mx0 = float(np.mean(x0))
    mx1 = float(np.mean(x1))
    var0 = float(np.var(x0))
    if var0 < 1e-18:
        return {"theta": 0.0, "mu": mx0, "sigma": 0.0, "b": 1.0}
    b = float(np.cov(x0, x1, ddof=0)[0, 1] / var0)
    b = float(max(1e-6, min(0.9999, b)))
    a = mx1 - b * mx0
    theta = -math.log(b) / max(dt, 1e-9)
    mu = a / (1.0 - b) if abs(1.0 - b) > 1e-9 else mx0
    resid = x1 - (a + b * x0)
    # σ_OU such that Var(ε) ≈ σ² (1-e^{-2θΔ})/(2θ)
    var_eps = float(np.var(resid))
    if theta > 1e-9:
        sigma = math.sqrt(max(0.0, var_eps * 2.0 * theta / max(1e-12, 1.0 - math.exp(-2.0 * theta * dt))))
    else:
        sigma = math.sqrt(max(var_eps, 0.0))
    return {"theta": float(theta), "mu": float(mu), "sigma": float(sigma), "b": b}


def hurst_rs(prices: Sequence[float], *, min_lag: int = 4) -> float:
    """Hurst exponent via rescaled range (R/S) on log-prices.

    H < 0.5 → mean-reversion; H > 0.5 → momentum; ≈0.5 random walk.
    """
    p = _as_float_array(prices)
    if p.size < max(16, min_lag * 4):
        return 0.5
    # Use log returns series as the process
    x = np.diff(np.log(np.maximum(p, 1e-12)))
    n = x.size
    if n < max(16, min_lag * 4):
        return 0.5
    lags = []
    rs_vals = []
    lag = min_lag
    while lag <= n // 2:
        k = n // lag
        if k < 2:
            break
        rs_chunk = []
        for i in range(k):
            seg = x[i * lag : (i + 1) * lag]
            if seg.size < 2:
                continue
            y = np.cumsum(seg - seg.mean())
            r = float(y.max() - y.min())
            s = float(seg.std(ddof=1)) if seg.size > 1 else 0.0
            if s > 1e-15:
                rs_chunk.append(r / s)
        if rs_chunk:
            lags.append(lag)
            rs_vals.append(float(np.mean(rs_chunk)))
        lag *= 2
    if len(lags) < 2:
        return 0.5
    log_l = np.log(_as_float_array(lags))
    log_rs = np.log(np.maximum(_as_float_array(rs_vals), 1e-12))
    var_l = float(np.var(log_l))
    if var_l < 1e-18:
        return 0.5
    h = float(np.cov(log_l, log_rs, ddof=0)[0, 1] / var_l)
    return float(max(0.05, min(0.95, h)))


def ou_mean_reversion_prob(price: float, ou: dict[str, float]) -> float:
    """P(UP) from OU pull: if price << μ, expect up-move (and vice versa)."""
    mu = float(ou.get("mu") or 0.0)
    sigma = float(ou.get("sigma") or 0.0)
    theta = float(ou.get("theta") or 0.0)
    if mu <= 0 or sigma <= 0 or theta <= 0:
        return 0.5
    # Standardized distance below/above mean → UP probability
    z = (mu - float(price)) / (sigma + 1e-12)
    # Soft map; stronger θ → trust reversion more
    strength = min(1.0, theta / 0.05)
    return _clamp01(0.5 + 0.15 * strength * float(np.tanh(z)))


# ---------------------------------------------------------------------------
# Online Kalman filter (scalar latent fair-prob / price state)
# ---------------------------------------------------------------------------

def kalman_filter_1d(
    observations: Sequence[float],
    *,
    q_proc: float = 1e-4,
    r_obs: float = 1e-3,
    x0: Optional[float] = None,
    p0: float = 1.0,
) -> tuple[float, list[float]]:
    """Scalar Kalman filter. Returns final state and full filtered path."""
    y = _as_float_array(observations)
    if y.size == 0:
        return 0.5, []
    x = float(x0 if x0 is not None else y[0])
    p = float(max(p0, 1e-8))
    path: list[float] = []
    for yt in y:
        # Predict
        p = p + q_proc
        # Update
        k = p / (p + r_obs)
        x = x + k * (float(yt) - x)
        p = (1.0 - k) * p
        path.append(x)
    return float(x), path


def kalman_prob_from_prices(
    prices: Sequence[float],
    *,
    window: int = 60,
) -> float:
    """Map Kalman-smoothed price vs last price → soft P(UP)."""
    p = _as_float_array(prices)
    if p.size < 5:
        return 0.5
    seg = p[-min(window, p.size) :]
    # Filter on returns for stationarity, then integrate
    rets = np.diff(seg) / np.maximum(seg[:-1], 1e-12)
    x, _ = kalman_filter_1d(rets, q_proc=1e-5, r_obs=5e-4)
    # Positive latent drift → UP
    return _clamp01(0.5 + 25.0 * x)


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def kl_divergence(p: float, q: float) -> float:
    """Binary KL(p || q)."""
    p = min(1.0 - 1e-9, max(1e-9, float(p)))
    q = min(1.0 - 1e-9, max(1e-9, float(q)))
    return float(p * math.log(p / q) + (1.0 - p) * math.log((1.0 - p) / (1.0 - q)))


def js_divergence(p: float, q: float) -> float:
    """Jensen–Shannon divergence (symmetric, bounded)."""
    m = 0.5 * (float(p) + float(q))
    return 0.5 * kl_divergence(p, m) + 0.5 * kl_divergence(q, m)


def fuse_probabilities(
    components: dict[str, float],
    weights: dict[str, float],
    *,
    p_market: float,
    swarm_weight: float = DEFAULT_SWARM_WEIGHT,
    market_blend: float = DEFAULT_MARKET_BLEND,
) -> float:
    """Confidence-weighted swarm then blend with market: 0.70 swarm + 0.30 market."""
    num = 0.0
    den = 0.0
    for name, p in components.items():
        w = float(weights.get(name, 0.0))
        if w <= 0:
            continue
        # Down-weight components that diverge hard from the market (noisy)
        js = js_divergence(p, p_market)
        w_eff = w * math.exp(-2.0 * js)
        num += w_eff * float(p)
        den += w_eff
    if den <= 0:
        p_swarm = 0.5
    else:
        p_swarm = num / den
    sw = float(swarm_weight)
    mb = float(market_blend)
    tot = sw + mb
    if tot <= 0:
        return _clamp01(p_swarm)
    sw, mb = sw / tot, mb / tot
    return _clamp01(sw * p_swarm + mb * float(p_market))


def momentum_to_q(momentum: float, timeframe: str = "5m") -> float:
    """Legacy toy map — kept as fallback / baseline component."""
    scale = 0.35 if timeframe == "5m" else 0.28
    return _clamp01(0.5 + float(momentum) * scale)


# Fallback confidence: when the ensemble lacks history it has little real
# signal, so its q is pulled toward the market (the best available estimate)
# and keeps only a damped momentum tilt. 0 = trust market fully, 1 = trust
# the toy momentum map fully. Kept low on purpose — a fallback that fires
# because data is thin must not assert a large edge.
FALLBACK_MOMENTUM_TRUST = 0.30


def anchor_fallback_q(baseline: float, p_market: float) -> float:
    """Blend the toy-momentum baseline toward the market for the fallback path."""
    p = float(p_market)
    tilt = float(baseline) - 0.5  # signed momentum tilt around neutral
    return _clamp01(p + FALLBACK_MOMENTUM_TRUST * tilt)


# ---------------------------------------------------------------------------
# Main ensemble
# ---------------------------------------------------------------------------

def ensemble_cex_implied_up(
    *,
    prices: Sequence[float],
    times: Sequence[float],
    momentum: float,
    timeframe: str,
    pm_implied_up: float,
    spot: float,
    strike: Optional[float] = None,
    seconds_to_resolution: float = 300.0,
    bids: Optional[Sequence[BookLevel | tuple[float, float]]] = None,
    asks: Optional[Sequence[BookLevel | tuple[float, float]]] = None,
    swarm_weight: float = DEFAULT_SWARM_WEIGHT,
    market_blend: float = DEFAULT_MARKET_BLEND,
    tf_windows: Sequence[float] = DEFAULT_TF_WINDOWS,
    tf_weights: Sequence[float] = DEFAULT_TF_WEIGHTS,
    enabled: bool = True,
) -> AdvancedSignalResult:
    """Hurst-gated multi-TF + OBI + log-normal + Kalman fusion.

    Falls back to ``momentum_to_q`` when history is too short or ``enabled=False``.
    """
    baseline = momentum_to_q(momentum, timeframe)
    p_mkt = float(pm_implied_up)
    if not enabled:
        return AdvancedSignalResult(
            q=anchor_fallback_q(baseline, p_mkt), used_fallback=True,
            reason="advanced_disabled", components={"momentum": baseline},
        )

    features: dict[str, float] = {}
    components: dict[str, float] = {"momentum": baseline}
    weights: dict[str, float] = {"momentum": 0.25}

    # --- Multi-TF slopes ---
    if len(prices) >= 5 and len(times) == len(prices):
        weighted, slopes = multi_tf_weighted_slope(
            times, prices, windows=tf_windows, weights=tf_weights
        )
        features.update(slopes)
        # Map weighted slope → P(UP); ~2% window move → strong
        q_mtf = _clamp01(0.5 + float(weighted) / 0.02 * 0.15)
        components["multi_tf"] = q_mtf
        weights["multi_tf"] = 0.25

    # --- Order book ---
    if bids and asks:
        ob = order_book_metrics(bids, asks, levels=5)
        features.update(ob)
        q_obi = obi_to_prob(ob["obi"])
        components["obi"] = q_obi
        weights["obi"] = 0.15

    # --- GARCH + log-normal ---
    p_arr = _as_float_array(prices)
    if p_arr.size >= 8:
        rets = np.diff(p_arr) / np.maximum(p_arr[:-1], 1e-12)
        # Annualize: assume ~1s sampling → 31_536_000 seconds/year (crypto 24/7)
        sigma_1s = estimate_garch11(rets)
        # Per-sqrt-second → annualized
        sigma_ann = sigma_1s * math.sqrt(31_536_000.0)
        features["garch_sigma_1s"] = float(sigma_1s)
        features["garch_sigma_ann"] = float(sigma_ann)
        T = max(1.0, float(seconds_to_resolution)) / (365.25 * 24 * 3600)
        K = float(strike if strike and strike > 0 else (spot or (p_arr[-1] if p_arr.size else 0)))
        S = float(spot if spot > 0 else (p_arr[-1] if p_arr.size else K))
        # For binary up/down windows, strike ≈ window-open ≈ current spot;
        # use slight moneyness from short momentum so signal isn't stuck at 0.5
        if abs(S - K) / max(S, 1e-9) < 1e-6:
            K = S * (1.0 - 0.0005 * float(np.sign(momentum) or 1.0))
        q_ln = lognormal_cex_prob(S, K, max(sigma_ann, 0.05), T)
        components["lognormal"] = q_ln
        weights["lognormal"] = 0.15

    # --- OU + Hurst regime ---
    regime = "unknown"
    if p_arr.size >= 16:
        ou = estimate_ou_ar1(p_arr, dt=1.0)
        features.update({f"ou_{k}": float(v) for k, v in ou.items()})
        h = hurst_rs(p_arr)
        features["hurst"] = h
        q_ou = ou_mean_reversion_prob(float(p_arr[-1]), ou)
        components["ou"] = q_ou
        if h < 0.45:
            regime = "mean_reversion"
            weights["ou"] = 0.25
            weights["multi_tf"] = weights.get("multi_tf", 0.0) * 0.5
            weights["momentum"] = weights.get("momentum", 0.0) * 0.5
        elif h > 0.55:
            regime = "momentum"
            weights["ou"] = 0.05
            weights["multi_tf"] = max(weights.get("multi_tf", 0.0), 0.30)
            weights["momentum"] = max(weights.get("momentum", 0.0), 0.25)
        else:
            regime = "random"
            weights["ou"] = 0.10

    # --- Kalman ---
    if p_arr.size >= 8:
        q_k = kalman_prob_from_prices(p_arr)
        components["kalman"] = q_k
        weights["kalman"] = 0.15
        features["kalman_q"] = q_k

    # Need at least one non-momentum component with real data
    non_mom = {k: v for k, v in components.items() if k != "momentum"}
    if not non_mom:
        return AdvancedSignalResult(
            q=anchor_fallback_q(baseline, p_mkt),
            used_fallback=True,
            reason="insufficient_history",
            components=components,
            features=features,
            regime=regime,
        )

    q_fused = fuse_probabilities(
        components,
        weights,
        p_market=p_mkt,
        swarm_weight=swarm_weight,
        market_blend=market_blend,
    )

    # Agreement bonus — OBI / Hurst / multi-TF pointing same way vs market
    agree = 0
    total = 0
    for name in ("obi", "multi_tf", "ou", "kalman", "lognormal"):
        if name not in components:
            continue
        total += 1
        if (components[name] - 0.5) * (q_fused - 0.5) > 0:
            agree += 1
    boost = 0.0
    if total > 0:
        boost = 0.10 * (agree / total)

    return AdvancedSignalResult(
        q=q_fused,
        conviction_boost=float(boost),
        regime=regime,
        components=components,
        features=features,
        meta={
            "weights": weights,
            "swarm_weight": swarm_weight,
            "market_blend": market_blend,
            "model_q_source": "advanced_ensemble",
        },
        used_fallback=False,
        reason=f"ensemble regime={regime} components={list(components)}",
    )
