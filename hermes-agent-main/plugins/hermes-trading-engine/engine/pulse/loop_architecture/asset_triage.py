"""Polymarket Asset Triage skill — Discovery Lane evaluator (PAPER ONLY).

See .grok/skills/polymarket-asset-triage/SKILL.md
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from engine.pulse.execution_gate import vwap_fill

logger = logging.getLogger("pulse.loop_architecture.asset_triage")

PROCEED_SWEEP = "PROCEED_SWEEP"
PROCEED_10X = "PROCEED_10X"


class TriageReject(str, Enum):
    NO_TV_SIGNAL = "REJECT_NO_TV_SIGNAL"
    NO_PRICE_TREND = "REJECT_NO_PRICE_TREND"
    TV_MISALIGNED = "REJECT_TV_MISALIGNED"
    TREND_MISALIGNED = "REJECT_TREND_MISALIGNED"
    WRONG_TIMEFRAME = "REJECT_WRONG_TIMEFRAME"
    PRICE_OUT_OF_BAND = "REJECT_PRICE_OUT_OF_BAND"
    INSUFFICIENT_DEPTH = "REJECT_INSUFFICIENT_DEPTH"
    MIN_SHARES = "REJECT_MIN_SHARES"
    NO_BREAKTHROUGH = "REJECT_NO_BREAKTHROUGH"


@dataclass
class TriageConfig:
    sweet_min: float = 0.47
    sweet_max: float = 0.55
    tail_max: float = 0.10
    min_depth_usd: float = 50.0
    max_slippage_pct: float = 2.0
    min_shares: float = 5.0
    tv_timeframes: tuple[str, ...] = ("5", "15", "30", "60", "240", "1440")
    tail_min_strength: float = 0.55
    tv_max_age_s: float = 3600.0
    trend_source: str = "price"  # price | tv

    @classmethod
    def from_env(cls, asset: str = "") -> "TriageConfig":
        def _f(k: str, d: float) -> float:
            try:
                return float(os.getenv(k, str(d)))
            except (TypeError, ValueError):
                return d

        def _asset_f(suffix: str, global_key: str, d: float) -> float:
            a = str(asset or "").strip().lower()
            if a in ("btc", "eth"):
                v = os.getenv(f"PULSE_TRIAGE_{a.upper()}_{suffix}")
                if v is not None and str(v).strip() != "":
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return _f(global_key, d)

        tfs = tuple(
            t.strip()
            for t in os.getenv(
                "PULSE_TV_MTF_TIMEFRAMES",
                "5,10,15,20,25,30,35,40,45,50,55,60",
            ).split(",")
            if t.strip()
        )
        trend_src = (os.getenv("PULSE_TRIAGE_TREND_SOURCE", "price") or "price").strip().lower()
        return cls(
            sweet_min=_asset_f("SWEET_MIN", "PULSE_TRIAGE_SWEET_MIN", 0.47),
            sweet_max=_asset_f("SWEET_MAX", "PULSE_TRIAGE_SWEET_MAX", 0.55),
            tail_max=_asset_f("TAIL_MAX", "PULSE_TRIAGE_TAIL_MAX", 0.10),
            min_depth_usd=_asset_f("MIN_DEPTH_USD", "PULSE_TRIAGE_MIN_DEPTH_USD", 50.0),
            max_slippage_pct=_asset_f("MAX_SLIPPAGE_PCT", "PULSE_TRIAGE_MAX_SLIPPAGE_PCT", 2.0),
            min_shares=_asset_f("MIN_SHARES", "PULSE_TRIAGE_MIN_SHARES", 5.0),
            tv_timeframes=tfs or ("5", "15", "30", "60", "240", "1440"),
            tail_min_strength=_asset_f(
                "TAIL_MIN_STRENGTH", "PULSE_TRIAGE_TAIL_MIN_STRENGTH", 0.55),
            tv_max_age_s=_asset_f("TV_MAX_AGE_S", "PULSE_TRIAGE_TV_MAX_AGE_S", 3600.0),
            trend_source=trend_src,
        )


@dataclass
class TriageVerdict:
    status: str
    side: str
    ask_price: float
    token_id: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    time_boundary: Optional[str] = None
    slippage_pct: Optional[float] = None
    shares_at_probe: Optional[float] = None
    detail: str = ""

    @property
    def proceed(self) -> bool:
        return self.status in (PROCEED_SWEEP, PROCEED_10X)


@dataclass
class RateLimitGuard:
    """HTTP 429 backoff — 5s base, max 3 retries (skill §4)."""

    max_retries: int = 3
    base_delay_s: float = 5.0
    attempts: int = 0
    exhausted: bool = False

    def note_429(self) -> bool:
        """Return True if caller should retry; False if exhausted (clean stop)."""
        self.attempts += 1
        if self.attempts > self.max_retries:
            self.exhausted = True
            logger.warning("asset_triage: rate-limit retries exhausted")
            return False
        delay = self.base_delay_s * (2 ** (self.attempts - 1))
        logger.info("asset_triage: 429 backoff %.1fs (attempt %d/%d)",
                    delay, self.attempts, self.max_retries)
        time.sleep(delay)
        return True


def _asset_key(symbol: Optional[str], window=None) -> str:
    sym = str(symbol or "").strip().upper()
    if sym.startswith("ETH"):
        return "eth"
    slug = str(getattr(window, "series_slug", "") or "").lower()
    if slug.startswith("eth") or "ethereum" in slug:
        return "eth"
    return "btc"


def _exploration_allows(env_key: str, default: str = "0") -> bool:
    """True when a Bernoulli probe at the configured rate should proceed (learning only)."""
    try:
        rate = float(os.getenv(env_key, default) or 0)
    except (TypeError, ValueError):
        return False
    return rate > 0 and random.random() < rate


def _trend_side_ok(trend: Optional[str], side: str) -> bool:
    from engine.pulse.price_action_trend import trend_aligns_side
    return trend_aligns_side(trend, side)


def _tv_side_ok(tv_dir: Optional[str], side: str) -> bool:
    if not tv_dir:
        return False
    d = str(tv_dir).strip().upper()
    if d in ("UP", "LONG", "BUY"):
        return side == "up"
    if d in ("DOWN", "SHORT", "SELL"):
        return side == "down"
    return False


def _depth_ok(book, *, probe_usd: float, max_slip_pct: float, min_shares: float) -> tuple[bool, float, float]:
    if book is None or book.best_ask is None or not book.asks:
        return False, 0.0, 0.0
    best = float(book.best_ask)
    vwap, spent, shares, fully = vwap_fill(book.asks, float(probe_usd))
    if vwap is None or best <= 0 or spent < probe_usd * 0.99:
        return False, 0.0, float(shares or 0)
    slip_pct = 100.0 * (float(vwap) - best) / best
    if slip_pct > max_slip_pct:
        return False, slip_pct, float(shares or 0)
    if float(shares or 0) < min_shares:
        return False, slip_pct, float(shares or 0)
    return True, slip_pct, float(shares or 0)


@dataclass
class AssetTriageSkill:
    """Discovery Lane skill — TV-triggered sweet-spot / tail triage."""

    cfg: TriageConfig = field(default_factory=TriageConfig.from_env)
    rate_guard: RateLimitGuard = field(default_factory=RateLimitGuard)
    proceed_sweep: int = 0
    proceed_10x: int = 0
    rejected: int = 0
    reject_reasons: dict = field(default_factory=dict)
    _asset_cfgs: dict = field(default_factory=dict, repr=False)

    def cfg_for(self, symbol: Optional[str] = None, window=None) -> TriageConfig:
        asset = _asset_key(symbol, window)
        if asset not in self._asset_cfgs:
            base = TriageConfig.from_env(asset)
            base = TriageConfig(
                sweet_min=base.sweet_min,
                sweet_max=base.sweet_max,
                tail_max=base.tail_max,
                min_depth_usd=base.min_depth_usd,
                max_slippage_pct=base.max_slippage_pct,
                min_shares=base.min_shares,
                tv_timeframes=self.cfg.tv_timeframes or base.tv_timeframes,
                tail_min_strength=base.tail_min_strength,
                tv_max_age_s=base.tv_max_age_s,
                trend_source=self.cfg.trend_source or base.trend_source,
            )
            self._asset_cfgs[asset] = base
        return self._asset_cfgs[asset]

    def _bump_reject(self, reason: str) -> None:
        self.rejected += 1
        self.reject_reasons[reason] = int(self.reject_reasons.get(reason, 0)) + 1

    def evaluate(
        self,
        *,
        window,
        side: str,
        ask_price: float,
        now: float,
        tv_feature: Optional[dict],
        symbol: Optional[str],
    ) -> TriageVerdict:
        """Run skill verification protocol before Execution Lane handoff."""
        cfg = self.cfg_for(symbol, window)
        trend_source = (self.cfg.trend_source or cfg.trend_source or "price").strip().lower()
        book = window.up_book if side == "up" else window.down_book
        token_id = (getattr(window, "up_token_id", None) if side == "up"
                    else getattr(window, "down_token_id", None))
        close_ts = getattr(window, "close_ts", None)
        time_boundary = str(close_ts) if close_ts is not None else ""

        if not tv_feature:
            reject = (TriageReject.NO_PRICE_TREND if trend_source == "price"
                      else TriageReject.NO_TV_SIGNAL)
            self._bump_reject(reject.value)
            return TriageVerdict(
                status=reject.value,
                side=side, ask_price=ask_price, token_id=token_id,
                symbol=symbol, time_boundary=time_boundary,
            )

        use_price = trend_source == "price" or tv_feature.get("source") == "price_action"
        tf = str(tv_feature.get("timeframe") or tv_feature.get("interval") or "")

        if not use_price:
            if tf not in cfg.tv_timeframes:
                self._bump_reject(TriageReject.WRONG_TIMEFRAME.value)
                return TriageVerdict(
                    status=TriageReject.WRONG_TIMEFRAME.value,
                    side=side, ask_price=ask_price, token_id=token_id,
                    symbol=symbol, timeframe=tf, time_boundary=time_boundary,
                    detail=f"tf={tf}",
                )

            age = tv_feature.get("age_s")
            if age is not None and float(age) > cfg.tv_max_age_s:
                self._bump_reject(TriageReject.NO_TV_SIGNAL.value)
                return TriageVerdict(
                    status=TriageReject.NO_TV_SIGNAL.value,
                    side=side, ask_price=ask_price, detail="tv_stale",
                )

            tv_dir = tv_feature.get("direction")
            if not _tv_side_ok(tv_dir, side):
                self._bump_reject(TriageReject.TV_MISALIGNED.value)
                return TriageVerdict(
                    status=TriageReject.TV_MISALIGNED.value,
                    side=side, ask_price=ask_price, token_id=token_id,
                    symbol=symbol, timeframe=tf, time_boundary=time_boundary,
                    detail=f"tv={tv_dir}",
                )
        else:
            tf = tf or "spot"
            trend = tv_feature.get("trend")
            if trend not in ("rising", "falling", "flat"):
                self._bump_reject(TriageReject.NO_PRICE_TREND.value)
                return TriageVerdict(
                    status=TriageReject.NO_PRICE_TREND.value,
                    side=side, ask_price=ask_price, token_id=token_id,
                    symbol=symbol, timeframe=tf, time_boundary=time_boundary,
                    detail="no_trend",
                )
            if trend == "flat":
                if not _exploration_allows("PULSE_TRIAGE_FLAT_EXPLORATION_RATE"):
                    self._bump_reject(TriageReject.TREND_MISALIGNED.value)
                    return TriageVerdict(
                        status=TriageReject.TREND_MISALIGNED.value,
                        side=side, ask_price=ask_price, token_id=token_id,
                        symbol=symbol, timeframe=tf, time_boundary=time_boundary,
                        detail="trend=flat",
                    )
            elif not _trend_side_ok(trend, side):
                if not _exploration_allows("PULSE_TRIAGE_TREND_EXPLORATION_RATE"):
                    self._bump_reject(TriageReject.TREND_MISALIGNED.value)
                    return TriageVerdict(
                        status=TriageReject.TREND_MISALIGNED.value,
                        side=side, ask_price=ask_price, token_id=token_id,
                        symbol=symbol, timeframe=tf, time_boundary=time_boundary,
                        detail=f"trend={trend}",
                    )

        ok_depth, slip_pct, shares = _depth_ok(
            book,
            probe_usd=cfg.min_depth_usd,
            max_slip_pct=cfg.max_slippage_pct,
            min_shares=cfg.min_shares,
        )
        if not ok_depth:
            self._bump_reject(TriageReject.INSUFFICIENT_DEPTH.value)
            return TriageVerdict(
                status=(TriageReject.MIN_SHARES.value if shares < cfg.min_shares
                        else TriageReject.INSUFFICIENT_DEPTH.value),
                side=side, ask_price=ask_price, token_id=token_id,
                symbol=symbol, timeframe=tf, time_boundary=time_boundary,
                slippage_pct=slip_pct, shares_at_probe=shares,
            )

        p = float(ask_price)
        strength = float(tv_feature.get("strength") or 0)

        if cfg.sweet_min <= p <= cfg.sweet_max:
            self.proceed_sweep += 1
            return TriageVerdict(
                status=PROCEED_SWEEP,
                side=side, ask_price=p, token_id=token_id,
                symbol=symbol, timeframe=tf, time_boundary=time_boundary,
                slippage_pct=slip_pct, shares_at_probe=shares,
            )

        if p < cfg.tail_max and strength >= cfg.tail_min_strength:
            self.proceed_10x += 1
            return TriageVerdict(
                status=PROCEED_10X,
                side=side, ask_price=p, token_id=token_id,
                symbol=symbol, timeframe=tf, time_boundary=time_boundary,
                slippage_pct=slip_pct, shares_at_probe=shares,
                detail="tail_breakthrough",
            )

        self._bump_reject(TriageReject.PRICE_OUT_OF_BAND.value)
        return TriageVerdict(
            status=TriageReject.PRICE_OUT_OF_BAND.value,
            side=side, ask_price=p, token_id=token_id,
            symbol=symbol, timeframe=tf, time_boundary=time_boundary,
        )

    def report(self) -> dict:
        per_asset = {}
        for asset in ("btc", "eth"):
            c = self.cfg_for(asset)
            per_asset[asset] = {
                "sweet_min": c.sweet_min,
                "sweet_max": c.sweet_max,
                "tail_max": c.tail_max,
                "min_depth_usd": c.min_depth_usd,
                "max_slippage_pct": c.max_slippage_pct,
                "min_shares": c.min_shares,
                "tv_max_age_s": c.tv_max_age_s,
            }
        return {
            "skill": "polymarket_asset_triage",
            "proceed_sweep": self.proceed_sweep,
            "proceed_10x": self.proceed_10x,
            "rejected": self.rejected,
            "reject_reasons": dict(self.reject_reasons),
            "thresholds": per_asset.get("btc", {}),
            "thresholds_by_asset": per_asset,
            "tv_timeframes": list(self.cfg.tv_timeframes),
            "trend_source": self.cfg.trend_source,
            "rate_limit": {
                "attempts": self.rate_guard.attempts,
                "exhausted": self.rate_guard.exhausted,
            },
        }


def with_rate_limit_retry(fn: Callable, guard: RateLimitGuard, *args, **kwargs) -> Any:
    """Call ``fn``; on 429-like errors, backoff up to skill max retries."""
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            if "429" not in msg and "rate" not in msg:
                raise
            if not guard.note_429():
                raise SystemExit(0) from exc
