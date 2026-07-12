"""OBSERVE-ONLY EP Chan-inspired research features for BTC 5-min pulse decisions.

These features are logged per candidate for research/diagnostics ONLY. They MUST NOT trade,
size, veto, or gate anything — the execution-quality gate remains the sole authority. Every
feature payload carries ``observe_only=True``. All estimators are pure-python and fail safe on
small samples / NaNs / missing data, returning an explicit diagnostic reason instead of a value.

Features:
  * rolling Hurst regime on BTC micro-returns -> trending | mean_reverting | noise
  * AR(1) half-life + ADF-style t-stat on the CEX-vs-Polymarket divergence series
  * z-score of the current CEX-implied-vs-Polymarket-price divergence
  * lightweight 1D Kalman fair-probability estimate (skipped with diagnostic if not enough data)
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# small, safe numeric helpers (no numpy dependency)
# --------------------------------------------------------------------------- #
def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not (math.isnan(x) or math.isinf(x))


def _clean(series) -> list:
    return [float(x) for x in (series or []) if _finite(x)]


def _mean(xs) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def _std(xs) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _ols(x: list, y: list) -> "tuple[Optional[float], Optional[float], Optional[float]]":
    """Ordinary least squares slope, intercept, slope-standard-error (pure python)."""
    n = len(x)
    if n < 3 or len(y) != n:
        return None, None, None
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    if sxx <= 1e-15:
        return None, None, None
    sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    slope = sxy / sxx
    intercept = my - slope * mx
    sse = sum((y[i] - (intercept + slope * x[i])) ** 2 for i in range(n))
    se = math.sqrt((sse / (n - 2)) / sxx) if (n > 2 and sxx > 0) else None
    return slope, intercept, se


# --------------------------------------------------------------------------- #
# estimators
# --------------------------------------------------------------------------- #
def hurst_exponent(returns, *, min_n: int = 32) -> Optional[float]:
    """Rescaled-range (R/S) Hurst exponent of a returns series. ~0.5 random, >0.5 persistent
    (trending), <0.5 anti-persistent (mean-reverting). Returns None when data is insufficient."""
    r = _clean(returns)
    n = len(r)
    if n < min_n:
        return None
    sizes = []
    s = 8
    while s <= n // 2:
        sizes.append(s)
        s *= 2
    if len(sizes) < 2:
        return None
    logs_n, logs_rs = [], []
    for size in sizes:
        rs_vals = []
        for start in range(0, n - size + 1, size):
            chunk = r[start:start + size]
            m = sum(chunk) / size
            dev = [c - m for c in chunk]
            cum, acc = [], 0.0
            for d in dev:
                acc += d
                cum.append(acc)
            rng = max(cum) - min(cum)
            sd = _std(chunk)
            if sd and sd > 0 and rng > 0:
                rs_vals.append(rng / sd)
        if rs_vals:
            logs_n.append(math.log(size))
            logs_rs.append(math.log(sum(rs_vals) / len(rs_vals)))
    if len(logs_n) < 2:
        return None
    slope, _, _ = _ols(logs_n, logs_rs)
    return slope


def classify_hurst(h: Optional[float], *, trend_th: float = 0.55,
                   revert_th: float = 0.45) -> str:
    if h is None or not _finite(h):
        return "insufficient_data"
    if h >= trend_th:
        return "trending"
    if h <= revert_th:
        return "mean_reverting"
    return "noise"


def half_life_adf(spread, *, min_n: int = 20) -> "tuple[Optional[float], Optional[float], str]":
    """AR(1)/Ornstein-Uhlenbeck half-life of mean reversion + an ADF-style t-stat on the
    spread series. Returns (half_life_s, adf_tstat, reason)."""
    s = _clean(spread)
    if len(s) < min_n:
        return None, None, "insufficient_samples"
    x = s[:-1]
    delta = [s[i + 1] - s[i] for i in range(len(s) - 1)]
    lam, _, se = _ols(x, delta)            # delta_t = lam * spread_{t-1} (+const)
    if lam is None:
        return None, None, "degenerate"
    adf_t = (lam / se) if (se and se > 0) else None
    b = 1.0 + lam
    if not (0.0 < b < 1.0):
        return None, adf_t, "no_mean_reversion"   # not stationary/reverting
    half = -math.log(2.0) / math.log(b)
    return (half if half > 0 else None), adf_t, "ok"


def zscore(value: Optional[float], buffer, *, min_n: int = 20) -> Optional[float]:
    b = _clean(buffer)
    if value is None or not _finite(value) or len(b) < min_n:
        return None
    m = _mean(b)
    sd = _std(b)
    if sd is None or sd <= 1e-12:
        return None
    return (value - m) / sd


def autocorrelation(series, *, lag: int = 1, min_n: int = 10) -> Optional[float]:
    """Lag-``lag`` autocorrelation of a series. None on insufficient/degenerate data."""
    s = _clean(series)
    n = len(s)
    if n < max(min_n, lag + 2):
        return None
    m = sum(s) / n
    denom = sum((x - m) ** 2 for x in s)
    if denom <= 1e-12:
        return None
    num = sum((s[i] - m) * (s[i - lag] - m) for i in range(lag, n))
    return max(-1.0, min(1.0, num / denom))


def realized_volatility(returns, *, min_n: int = 10) -> Optional[float]:
    """Realized volatility = sample std of the returns series. None if too few samples."""
    r = _clean(returns)
    if len(r) < min_n:
        return None
    return _std(r)


def zscore_bucket(z: Optional[float]) -> str:
    if z is None or not _finite(z):
        return "na"
    if z <= -2:
        return "<=-2"
    if z <= -1:
        return "-2..-1"
    if z < 1:
        return "-1..1"
    if z < 2:
        return "1..2"
    return ">=2"


def kalman_fair_prob(obs, *, q: float = 1e-4, r: float = 1e-2,
                     min_n: int = 8) -> "tuple[Optional[float], str]":
    """Lightweight, stable 1D random-walk Kalman smoother over a fair-probability series.
    Skips (None + reason) when there isn't enough data."""
    o = _clean(obs)
    if len(o) < min_n:
        return None, "insufficient_samples"
    x = o[0]
    p = 1.0
    for z in o[1:]:
        p += q
        k = p / (p + r)
        x = x + k * (z - x)
        p = (1.0 - k) * p
    return max(0.0, min(1.0, x)), "ok"


@dataclass
class ResearchFeatures:
    observe_only: bool = True
    hurst: Optional[float] = None
    hurst_regime: str = "insufficient_data"
    half_life_s: Optional[float] = None
    adf_tstat: Optional[float] = None
    half_life_reason: str = "insufficient_samples"
    divergence: Optional[float] = None
    zscore: Optional[float] = None
    zscore_bucket: str = "na"
    kalman_fair_prob: Optional[float] = None
    kalman_reason: str = "insufficient_samples"
    autocorr_lag1: Optional[float] = None
    realized_vol: Optional[float] = None
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"observe_only": True,
                "hurst": (round(self.hurst, 4) if self.hurst is not None else None),
                "hurst_regime": self.hurst_regime,
                "autocorr_lag1": (round(self.autocorr_lag1, 4)
                                  if self.autocorr_lag1 is not None else None),
                "realized_vol": (round(self.realized_vol, 8)
                                 if self.realized_vol is not None else None),
                "half_life_s": (round(self.half_life_s, 2) if self.half_life_s is not None else None),
                "adf_tstat": (round(self.adf_tstat, 4) if self.adf_tstat is not None else None),
                "half_life_reason": self.half_life_reason,
                "divergence": (round(self.divergence, 6) if self.divergence is not None else None),
                "zscore": (round(self.zscore, 4) if self.zscore is not None else None),
                "zscore_bucket": self.zscore_bucket,
                "kalman_fair_prob": (round(self.kalman_fair_prob, 4)
                                     if self.kalman_fair_prob is not None else None),
                "kalman_reason": self.kalman_reason,
                "diagnostics": dict(self.diagnostics)}


class ResearchObservatory:
    """Holds rolling buffers + computes per-candidate observe-only features, tracks coverage +
    missing-data reasons, and aggregates PnL/calibration grouped by regime and z-score bucket.
    Nothing here can affect a trade decision."""

    def __init__(self, *, returns_window: int = 300, div_window: int = 300,
                 returns_min: int = 32, div_min: int = 20):
        self.returns_min = int(returns_min)
        self.div_min = int(div_min)
        self._prices: deque = deque(maxlen=int(returns_window))   # oracle prices
        self._div: deque = deque(maxlen=int(div_window))          # CEX-vs-Poly divergence
        self._cex_implied: deque = deque(maxlen=int(div_window))  # CEX-implied fair prob
        self.coverage = {
            "candidates": 0, "hurst_present": 0, "half_life_present": 0,
            "zscore_present": 0, "kalman_present": 0,
            "missing_reasons": {}}
        self.by_regime: dict = {}
        self.by_zbucket: dict = {}
        self.by_half_life: dict = {}
        self.by_ttc: dict = {}

    # -- ingest ------------------------------------------------------------- #
    def observe_oracle(self, price: Optional[float]) -> None:
        # dedupe: the Chainlink oracle updates less often than the 1s price sampler, so only
        # record genuine price CHANGES — otherwise the returns series is mostly zeros and Hurst
        # is undefined. Observe-only; this never affects the trading sigma/decision.
        if price is not None and _finite(price) and price > 0:
            p = float(price)
            if not self._prices or self._prices[-1] != p:
                self._prices.append(p)

    def observe_divergence(self, divergence: Optional[float],
                           cex_implied: Optional[float]) -> None:
        if divergence is not None and _finite(divergence):
            self._div.append(float(divergence))
        if cex_implied is not None and _finite(cex_implied):
            self._cex_implied.append(float(cex_implied))

    def _returns(self) -> list:
        p = list(self._prices)
        out = []
        for i in range(1, len(p)):
            if p[i - 1] > 0 and p[i] > 0:
                out.append(math.log(p[i] / p[i - 1]))
        return out

    def _bump_missing(self, reason: str) -> None:
        self.coverage["missing_reasons"][reason] = \
            self.coverage["missing_reasons"].get(reason, 0) + 1

    # -- per-candidate evaluation (observe only) ---------------------------- #
    def evaluate(self, *, current_divergence: Optional[float] = None) -> ResearchFeatures:
        self.coverage["candidates"] += 1
        f = ResearchFeatures()
        rets = self._returns()
        f.hurst = hurst_exponent(rets, min_n=self.returns_min)
        f.hurst_regime = classify_hurst(f.hurst)
        f.autocorr_lag1 = autocorrelation(rets, lag=1)
        f.realized_vol = realized_volatility(rets)
        if f.hurst is not None:
            self.coverage["hurst_present"] += 1
        else:
            f.diagnostics["hurst"] = "insufficient_returns"
            self._bump_missing("hurst:insufficient_returns")

        half, adf, reason = half_life_adf(list(self._div), min_n=self.div_min)
        f.half_life_s, f.adf_tstat, f.half_life_reason = half, adf, reason
        if half is not None:
            self.coverage["half_life_present"] += 1
        else:
            f.diagnostics["half_life"] = reason
            self._bump_missing("half_life:" + reason)

        f.divergence = current_divergence
        f.zscore = zscore(current_divergence, list(self._div), min_n=self.div_min)
        f.zscore_bucket = zscore_bucket(f.zscore)
        if f.zscore is not None:
            self.coverage["zscore_present"] += 1
        else:
            f.diagnostics["zscore"] = "insufficient_divergence_samples"
            self._bump_missing("zscore:insufficient_divergence_samples")

        kf, kreason = kalman_fair_prob(list(self._cex_implied))
        f.kalman_fair_prob, f.kalman_reason = kf, kreason
        if kf is not None:
            self.coverage["kalman_present"] += 1
        else:
            f.diagnostics["kalman"] = kreason
            self._bump_missing("kalman:" + kreason)
        return f

    # -- grouped outcome aggregation (post-settlement) ---------------------- #
    def record_settled(self, *, regime: Optional[str], zbucket: Optional[str],
                        pnl: float, won: bool, fair_at_entry: Optional[float],
                        outcome_up: Optional[bool], half_life_bucket: Optional[str] = None,
                        ttc_bucket: Optional[str] = None) -> None:
        def _acc(d, key):
            g = d.setdefault(key or "unknown", {"n": 0, "wins": 0, "pnl": 0.0, "brier_sum": 0.0,
                                                "brier_n": 0})
            g["n"] += 1
            g["wins"] += int(bool(won))
            g["pnl"] = round(g["pnl"] + float(pnl), 6)
            if fair_at_entry is not None and outcome_up is not None:
                y = 1.0 if outcome_up else 0.0
                g["brier_sum"] += (float(fair_at_entry) - y) ** 2
                g["brier_n"] += 1
        _acc(self.by_regime, regime)
        _acc(self.by_zbucket, zbucket)
        _acc(self.by_half_life, half_life_bucket)
        _acc(self.by_ttc, ttc_bucket)

    @staticmethod
    def _summ(groups: dict) -> dict:
        out = {}
        for k, g in groups.items():
            out[k] = {"n": g["n"], "win_rate": (round(g["wins"] / g["n"], 4) if g["n"] else None),
                      "pnl_usd": round(g["pnl"], 4),
                      "brier": (round(g["brier_sum"] / g["brier_n"], 4) if g["brier_n"] else None)}
        return out

    def report(self) -> dict:
        return {"observe_only": True, "affects_trading": False,
                "coverage": {k: v for k, v in self.coverage.items() if k != "missing_reasons"},
                "missing_data_reasons": dict(self.coverage["missing_reasons"]),
                "divergence_samples": len(self._div), "return_samples": len(self._prices),
                "pnl_by_regime": self._summ(self.by_regime),
                "pnl_by_zscore_bucket": self._summ(self.by_zbucket),
                "pnl_by_half_life_bucket": self._summ(self.by_half_life),
                "pnl_by_ttc_bucket": self._summ(self.by_ttc)}
