"""Digital fair value for a BTC 5-minute up/down window (PAPER research math).

The contract resolves ``Up`` iff ``S_close >= S_open``. Mid-window, with the current
price ``S_now`` and ``r`` seconds remaining, and a per-second log-return volatility
``sigma``, model the close as ``S_close = S_now * exp((mu-0.5 sigma^2) r + sigma sqrt(r) Z)``
so::

    P(Up) = P(ln(S_close/S_open) >= 0)
          = Phi( (ln(S_now/S_open) + (mu-0.5 sigma^2) r) / (sigma sqrt(r)) )

This is the price of a digital/binary option. As ``r -> 0`` it collapses to the realized
sign (1 if up so far, else 0) — the basis of the late-window nowcasting edge. Drift ``mu``
defaults to 0 (5-min BTC is ~driftless); a tiny momentum drift may be supplied.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def digital_p_up(s_now: float, s_open: float, sigma_per_sec: float, r_seconds: float,
                 *, mu_per_sec: float = 0.0) -> Optional[float]:
    """P(close >= open) for the remaining ``r_seconds``. Returns None when undefined."""
    if s_now is None or s_open is None or s_now <= 0 or s_open <= 0:
        return None
    if r_seconds <= 0:                       # window over -> realized digital (ties -> Up)
        return 1.0 if s_now >= s_open else 0.0
    if sigma_per_sec is None or sigma_per_sec <= 0:
        return None
    sig_h = sigma_per_sec * math.sqrt(r_seconds)
    if sig_h <= 1e-12:
        return 1.0 if s_now >= s_open else 0.0
    z = (math.log(s_now / s_open) + (mu_per_sec - 0.5 * sigma_per_sec ** 2) * r_seconds) / sig_h
    return max(0.0, min(1.0, _norm_cdf(z)))


class RollingVol:
    """Rolling per-second BTC log-return volatility from a price stream."""

    def __init__(self, *, window_s: float = 900.0, max_samples: int = 5000,
                 min_samples: int = 8, floor_per_sec: float = 1e-6):
        self.window_s = float(window_s)
        self.min_samples = int(min_samples)
        self.floor_per_sec = float(floor_per_sec)
        self._buf: deque = deque(maxlen=int(max_samples))

    def observe(self, price: Optional[float], now: float) -> None:
        if price is None:
            return
        try:
            p = float(price)
        except (TypeError, ValueError):
            return
        if p > 0:
            self._buf.append((float(now), p))

    def per_sec(self, now: Optional[float] = None) -> Optional[float]:
        snap = list(self._buf)
        if len(snap) < self.min_samples:
            return None
        if now is not None:
            snap = [(t, p) for t, p in snap if now - t <= self.window_s] or snap
        rets = []
        for (t0, p0), (t1, p1) in zip(snap, snap[1:]):
            dt = t1 - t0
            if dt > 0 and p0 > 0 and p1 > 0:
                rets.append(math.log(p1 / p0) / math.sqrt(dt))   # per-sqrt-second return
        if len(rets) < self.min_samples - 1:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
        return max(self.floor_per_sec, math.sqrt(var))

    @property
    def samples(self) -> int:
        return len(self._buf)
