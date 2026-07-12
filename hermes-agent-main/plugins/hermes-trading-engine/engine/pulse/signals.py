"""Simons-style layered raw-signal detection for the BTC 5-min pulse (OBSERVE-ONLY).

Builds a multi-horizon raw signal snapshot per candidate from BTC 15s/30s/60s/180s returns,
realized vol, autocorrelation, (optional) TradingView direction, Polymarket YES price movement,
spread change, and depth change — then outputs a directional read: direction, strength,
confidence, and signal family. This is a detection LAYER (many weak reads), not one indicator.

OBSERVE-ONLY: snapshots are logged + summarized; they do not trade, size, veto, or bypass the
execution gate. Pure-python, NaN/small-sample safe.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from engine.pulse.research_features import autocorrelation, realized_volatility


@dataclass
class SignalSnapshot:
    observe_only: bool = True
    returns: dict = field(default_factory=dict)         # {"15s":r, "30s":r, "60s":r, "180s":r}
    realized_vol: Optional[float] = None
    autocorr_lag1: Optional[float] = None
    tv_direction: Optional[str] = None                  # None unless a TradingView feed exists
    poly_move: Optional[float] = None
    spread_change: Optional[float] = None
    depth_change: Optional[float] = None
    ttc_s: Optional[float] = None
    direction: str = "neutral"                          # up | down | neutral
    strength: float = 0.0                               # 0..1
    confidence: float = 0.0                             # 0..1
    family: str = "insufficient_data"                   # momentum|mean_reversion|microstructure|noise|insufficient_data

    def to_dict(self) -> dict:
        return {"observe_only": True,
                "returns": {k: (round(v, 8) if v is not None else None)
                            for k, v in self.returns.items()},
                "realized_vol": (round(self.realized_vol, 8) if self.realized_vol is not None else None),
                "autocorr_lag1": (round(self.autocorr_lag1, 4) if self.autocorr_lag1 is not None else None),
                "tv_direction": self.tv_direction, "poly_move": self.poly_move,
                "spread_change": self.spread_change, "depth_change": self.depth_change,
                "ttc_s": (round(self.ttc_s, 1) if self.ttc_s is not None else None),
                "direction": self.direction, "strength": round(self.strength, 4),
                "confidence": round(self.confidence, 4), "family": self.family}


class SignalEngine:
    """Maintains timestamped price + Polymarket-microstructure buffers and emits an observe-only
    multi-horizon SignalSnapshot per candidate."""

    def __init__(self, *, horizons=(15, 30, 60, 180), window_s: float = 300.0,
                 max_samples: int = 6000):
        self.horizons = tuple(horizons)
        self.window_s = float(window_s)
        self._px: deque = deque(maxlen=int(max_samples))      # (ts, price)
        self._poly: deque = deque(maxlen=int(max_samples))    # (ts, poly_yes, spread, depth)
        self.coverage = {"snapshots": 0, "by_family": {}, "by_direction": {}}

    def observe_price(self, price: Optional[float], now: float) -> None:
        try:
            p = float(price)
        except (TypeError, ValueError):
            return
        if p > 0:
            self._px.append((float(now), p))

    def observe_poly(self, poly_yes: Optional[float], spread: Optional[float],
                     depth: Optional[float], now: float) -> None:
        self._poly.append((float(now), poly_yes, spread, depth))

    def _ret_over(self, horizon_s: float, now: float) -> Optional[float]:
        snap = list(self._px)
        if len(snap) < 2:
            return None
        cur = snap[-1][1]
        target = now - horizon_s
        past = None
        for ts, p in snap:                # earliest sample at/after the target time
            if ts >= target:
                past = p
                break
        if past is None or past <= 0 or cur <= 0:
            return None
        return math.log(cur / past)

    def _poly_changes(self, now: float) -> "tuple[Optional[float], Optional[float], Optional[float]]":
        win = [r for r in self._poly if now - r[0] <= 60.0]    # last ~60s of microstructure
        if len(win) < 2:
            return None, None, None
        first, last = win[0], win[-1]

        def _d(i):
            a, b = first[i], last[i]
            return (b - a) if (a is not None and b is not None) else None
        return _d(1), _d(2), _d(3)        # poly_move, spread_change, depth_change

    def snapshot(self, *, ttc_s: Optional[float], now: float) -> SignalSnapshot:
        s = SignalSnapshot(ttc_s=ttc_s)
        rets = {f"{h}s": self._ret_over(h, now) for h in self.horizons}
        s.returns = rets
        px = [p for _, p in self._px]
        log_rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))
                    if px[i - 1] > 0 and px[i] > 0]
        s.realized_vol = realized_volatility(log_rets)
        s.autocorr_lag1 = autocorrelation(log_rets, lag=1)
        s.poly_move, s.spread_change, s.depth_change = self._poly_changes(now)
        present = [v for v in rets.values() if v is not None]
        if not present:
            self._bump(s)
            return s
        # blended multi-horizon momentum score, normalized by realized vol
        vol = s.realized_vol or 1e-6
        votes = [math.tanh(r / (vol * 8.0)) for r in present]   # bounded per-horizon vote
        score = sum(votes) / len(votes)
        s.strength = min(1.0, abs(score))
        if score > 0.15:
            s.direction = "up"
        elif score < -0.15:
            s.direction = "down"
        else:
            s.direction = "neutral"
        # confidence: cross-horizon agreement * data sufficiency
        same = sum(1 for v in votes if (v >= 0) == (score >= 0))
        agreement = same / len(votes)
        data_ok = len(present) / max(1, len(self.horizons))
        s.confidence = round(max(0.0, min(1.0, agreement * data_ok)), 4)
        # family classification
        short = rets.get(f"{self.horizons[0]}s")
        long = rets.get(f"{self.horizons[-1]}s")
        micro = max(abs(s.spread_change or 0.0), abs((s.depth_change or 0.0)) / 1e6)
        if short is not None and long is not None and short != 0 and long != 0 \
                and (short > 0) != (long > 0):
            s.family = "mean_reversion"
        elif agreement >= 0.75 and s.direction != "neutral":
            s.family = "momentum"
        elif micro > 0.01:
            s.family = "microstructure"
        else:
            s.family = "noise"
        self._bump(s)
        return s

    def _bump(self, s: SignalSnapshot) -> None:
        self.coverage["snapshots"] += 1
        self.coverage["by_family"][s.family] = self.coverage["by_family"].get(s.family, 0) + 1
        self.coverage["by_direction"][s.direction] = \
            self.coverage["by_direction"].get(s.direction, 0) + 1

    def report(self) -> dict:
        return {"enabled": True, "observe_only": True, "affects_trading": False,
                "horizons_s": list(self.horizons),
                "tv_direction_available": False, **self.coverage}
