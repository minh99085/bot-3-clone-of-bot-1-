"""TradingView indicator-alert intake for the BTC 5-min pulse (OBSERVE-ONLY).

TradingView alerts feed Hermes **candidate signals only**. A TradingView alert can NEVER:
directly place a trade, resize a trade, bypass the strategy/execution gate, or override the
Polymarket orderbook checks. It is normalized into a ``TradingViewSignalEvent`` and attached to
candidates as an observe-only external feature; whether a paper trade happens is decided solely
by the existing Hermes strategy + the strict execution-quality gate.

This module is pure (no sockets) so it is fully unit-testable; the HTTP listener lives in
``engine/pulse/webhook.py`` and simply calls :meth:`TradingViewIntake.ingest`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("hte.pulse.tradingview")

# explicit, stable rejection reasons (acceptance criterion #3 + #8)
INVALID_JSON = "invalid_json"
MISSING_SECRET = "missing_secret"
BAD_SECRET = "bad_secret"
WRONG_BOT = "wrong_bot_name"
UNSUPPORTED_SYMBOL = "unsupported_symbol"
STALE_TIMESTAMP = "stale_timestamp"
MALFORMED_DIRECTION = "malformed_direction"
DUPLICATE_EVENT_ID = "duplicate_event_id"
WRONG_EVENT_SUFFIX = "wrong_event_id_suffix"
NOT_OBJECT = "payload_not_object"
REJECT_REASONS = (INVALID_JSON, MISSING_SECRET, BAD_SECRET, WRONG_BOT, UNSUPPORTED_SYMBOL,
                  STALE_TIMESTAMP, MALFORMED_DIRECTION, DUPLICATE_EVENT_ID, WRONG_EVENT_SUFFIX,
                  NOT_OBJECT)
# Retired intrahour TFs — purged from persisted snapshots on load. Empty: operator RSI-div
# ladder uses 5–55m + 60m on BTC/ETH charts; nothing retired in this ladder.
RETIRED_MTF_TFS = frozenset()
# Chart timeframes to strip from persisted snapshots on load. Empty now: 5m/10m/15m are ACTIVE
# horizon-matched TFs (operator added those charts; each is a graded per-TF council member), so they
# must NOT be pruned. (Was {"5","10","15"} from the pre-2m/3m/4m era, which silently dropped them.)
LEGACY_MTF_TFS = RETIRED_MTF_TFS

_DIRECTION_MAP = {
    "up": "UP", "long": "UP", "buy": "UP", "bull": "UP", "bullish": "UP", "1": "UP",
    "down": "DOWN", "short": "DOWN", "sell": "DOWN", "bear": "DOWN", "bearish": "DOWN", "-1": "DOWN",
    "flat": "FLAT", "neutral": "FLAT", "none": "FLAT", "close": "FLAT", "exit": "FLAT", "0": "FLAT",
}


def normalize_direction(raw) -> Optional[str]:
    if raw is None:
        return None
    return _DIRECTION_MAP.get(str(raw).strip().lower())


# BTC ticker aliases collapsed to ``feature_symbol`` (default BTCUSD / INDEX) for storage.
_BTC_SYMBOL_ALIASES = frozenset({"BTC/USD", "BTC", "XBTUSD", "BTCUSD"})


def canonical_storage_symbol(symbol: Optional[str], feature_symbol: str = "BTCUSD") -> str:
    """Map BTC-family tickers to the configured chart symbol for counters/history keys."""
    sym = normalize_symbol(symbol)
    feat = normalize_symbol(feature_symbol) or "BTCUSD"
    if sym in _BTC_SYMBOL_ALIASES:
        return feat
    return sym or feat


def normalize_symbol(raw) -> str:
    """Uppercase + strip a leading ``EXCHANGE:`` prefix so TradingView ``{{ticker}}`` values like
    ``INDEX:BTCUSD`` / ``COINBASE:BTCUSD`` match the allow-list (``BTCUSD``)."""
    s = str(raw or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    return s


# Lane chart routing (operator 2026-07-11):
#   1h Polymarket windows  -> Binance *USDT charts (leading liquidity)
#   15m Polymarket windows -> Chainlink INDEX *USD charts (settlement oracle)
TV_CHART_LANE_1H = "1h"
TV_CHART_LANE_15M = "15m"
TV_CHART_LANE_5M = "5m"


def tv_asset_from_blob(series_slug: Optional[str] = None, *,
                       market_slug: Optional[str] = None,
                       series_label: Optional[str] = None) -> Optional[str]:
    """Underlying asset key: ``btc`` | ``eth`` (None for unsupported / above-strike)."""
    label = str(series_label or "").strip().lower()
    if label.endswith("_above"):
        return None
    blob = " ".join((str(series_slug or ""), str(market_slug or ""),
                     str(series_label or ""))).lower()
    if "ethereum" in blob or blob.startswith("eth") or "eth_" in blob:
        return "eth"
    if "bitcoin" in blob or blob.startswith("btc") or "btc_" in blob:
        return "btc"
    s = str(series_slug or "").lower()
    if s.startswith("eth"):
        return "eth"
    if s.startswith("btc"):
        return "btc"
    return None


def tv_lane_kind(*, window_seconds: Optional[int] = None,
                 series_slug: Optional[str] = None,
                 series_label: Optional[str] = None) -> str:
    """Chart lane for TV FIFO routing: ``1h`` (USDT) vs ``15m``/``5m`` (INDEX USD)."""
    from engine.pulse.hourly_entry_timing import is_hourly_window

    slug = str(series_slug or "").lower()
    label = str(series_label or "").lower()
    ws = int(window_seconds or 0)
    if is_hourly_window(ws):
        return TV_CHART_LANE_1H
    if 600 <= ws <= 1200 or "15m" in slug or label.endswith("_15m"):
        return TV_CHART_LANE_15M
    if ws <= 300 or slug.endswith("-5m") or label.endswith("_5m"):
        return TV_CHART_LANE_5M
    return TV_CHART_LANE_15M if ws < 3600 else TV_CHART_LANE_1H


def tv_chart_symbol_for_asset(asset: Optional[str], lane: str) -> Optional[str]:
    """Map asset + lane to the single TradingView storage symbol (no cross-feed)."""
    a = str(asset or "").strip().lower()
    if a not in ("btc", "eth"):
        return None
    if lane == TV_CHART_LANE_1H:
        return "ETHUSDT" if a == "eth" else "BTCUSDT"
    return "ETHUSD" if a == "eth" else "BTCUSD"


def tv_chart_symbol_for_window(
    window=None,
    *,
    series_slug: Optional[str] = None,
    market_slug: Optional[str] = None,
    series_label: Optional[str] = None,
    window_seconds: Optional[int] = None,
    default_btc: str = "BTCUSD",
) -> Optional[str]:
    """Lane-aware TV chart symbol: 1h -> *USDT, 15m -> INDEX *USD."""
    lane_sym = getattr(window, "tv_symbol", None) if window is not None else None
    if lane_sym:
        return normalize_symbol(lane_sym)
    slug = series_slug if series_slug is not None else getattr(window, "series_slug", "")
    mslug = market_slug if market_slug is not None else getattr(window, "slug", "")
    label = series_label if series_label is not None else getattr(window, "series_label", "")
    ws = window_seconds if window_seconds is not None else int(
        getattr(window, "window_seconds", 0) or 0)
    asset = tv_asset_from_blob(slug, market_slug=mslug, series_label=label)
    if not asset:
        return None
    lane = tv_lane_kind(window_seconds=ws, series_slug=slug, series_label=label)
    return tv_chart_symbol_for_asset(asset, lane) or normalize_symbol(default_btc)


def tv_lane_metadata_for_window(window=None, **kwargs) -> dict:
    """Compact lane routing block for Grok/status (chart feed + settlement alignment)."""
    slug = kwargs.get("series_slug")
    if slug is None and window is not None:
        slug = getattr(window, "series_slug", "")
    label = kwargs.get("series_label")
    if label is None and window is not None:
        label = getattr(window, "series_label", "")
    ws = kwargs.get("window_seconds")
    if ws is None and window is not None:
        ws = int(getattr(window, "window_seconds", 0) or 0)
    lane = tv_lane_kind(window_seconds=ws, series_slug=slug, series_label=label)
    sym = tv_chart_symbol_for_window(
        window, series_slug=slug, series_label=label, window_seconds=ws)
    feed = "binance_usdt" if lane == TV_CHART_LANE_1H else "chainlink_index_usd"
    return {
        "lane": lane,
        "chart_symbol": sym,
        "feed": feed,
        "note": ("1h lane reads *USDT FIFO only; 15m lane reads INDEX *USD FIFO only "
                 "(bar-close + RSI never cross-feed)."),
    }


def tv_symbol_for_series_slug(series_slug: Optional[str],
                              *, market_slug: Optional[str] = None,
                              series_label: Optional[str] = None,
                              window_seconds: Optional[int] = None) -> Optional[str]:
    """Map a Polymarket series slug to a lane-aware TV storage symbol.

    Without ``window_seconds``, defaults to the 1h/USDT chart for hourly slugs and INDEX USD
    for 15m/5m slugs (slug-inferred lane).
    """
    slug = str(series_slug or "")
    label = str(series_label or "")
    ws = window_seconds
    if ws is None:
        low = slug.lower()
        if "hourly" in low or "1h" in label.lower():
            ws = 3600
        elif "15m" in low or label.endswith("_15m"):
            ws = 900
        elif "5m" in low or label.endswith("_5m"):
            ws = 300
    return tv_chart_symbol_for_window(
        None,
        series_slug=slug,
        market_slug=market_slug,
        series_label=label,
        window_seconds=ws,
    )


def tv_symbol_for_window(
    window,
    *,
    series_slug: Optional[str] = None,
    market_slug: Optional[str] = None,
    series_label: Optional[str] = None,
    default_btc: str = "BTCUSD",
) -> Optional[str]:
    """Resolve lane-aware TV storage symbol for a trading window."""
    sym = tv_chart_symbol_for_window(
        window,
        series_slug=series_slug,
        market_slug=market_slug,
        series_label=series_label,
        default_btc=default_btc,
    )
    return sym or normalize_symbol(default_btc)


DEFAULT_MTF_TIMEFRAMES = ("5", "10", "15", "20", "25", "30", "35", "40", "45", "50", "55", "60")
DEFAULT_MTF_CONFIRM_WINDOWS = {
    "2": 300.0,
    "3": 1200.0,
    "4": 1500.0,
    "5": 1500.0,
    "10": 1500.0,
    "15": 2250.0,
    "30": 4500.0,
    "45": 6750.0,
    "55": 8250.0,
}


def parse_mtf_timeframes(raw) -> tuple[str, ...]:
    """Parse ``PULSE_TV_MTF_TIMEFRAMES`` (e.g. ``5,10,15``) into canonical minute keys."""
    if raw is None or not str(raw).strip():
        return DEFAULT_MTF_TIMEFRAMES
    out: list[str] = []
    for part in str(raw).split(","):
        tf = normalize_timeframe(part.strip())
        if tf and tf not in out:
            out.append(tf)
    return tuple(out) if out else DEFAULT_MTF_TIMEFRAMES


def tf_label(tf: str) -> str:
    return "%sm" % str(tf)


def tf_dir_key(tf: str) -> str:
    return "tf_%sm_dir" % str(tf)


def tf_age_key(tf: str) -> str:
    return "tf_%sm_age_s" % str(tf)


def build_mtf_confirm_windows(
    mtf_timeframes: tuple[str, ...],
    *,
    legacy_5m_s: float = 360.0,
    legacy_10m_s: float = 660.0,
    legacy_15m_s: float = 960.0,
    overrides: Optional[dict] = None,
) -> dict[str, float]:
    """Per-TF freshness windows (seconds) for MTF confirmation."""
    windows = dict(DEFAULT_MTF_CONFIRM_WINDOWS)
    windows["5"] = float(legacy_5m_s)
    windows["10"] = float(legacy_10m_s)
    windows["15"] = float(legacy_15m_s)
    if overrides:
        for tf, val in overrides.items():
            if val is not None:
                windows[str(tf)] = float(val)
    out: dict[str, float] = {}
    for tf in mtf_timeframes:
        if tf in windows:
            out[tf] = windows[tf]
        else:
            try:
                # ~2.5 chart bars: selective Pine alerts may skip bars without a scored signal.
                out[tf] = float(int(tf) * 60 * 2.5)
            except ValueError:
                out[tf] = 360.0
    return out


def normalize_timeframe(raw) -> Optional[str]:
    """Canonical minute key for per-TF storage (``10``, ``10m``, ``10min`` -> ``10``).

    Each timeframe is stored separately in ``latest_by_tf`` so 5m/10m/15m alerts never
    overwrite one another — only the matching TF slot is updated."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    for suffix in ("minutes", "minute", "min", "m"):
        if s.endswith(suffix):
            core = s[: -len(suffix)].strip()
            if core.isdigit():
                s = core
                break
    return s if s else None


def _parse_ts(val) -> Optional[float]:
    """Parse an epoch (s or ms) or ISO-8601 timestamp into epoch seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        return v / 1000.0 if v > 1e11 else v          # ms -> s heuristic
    s = str(val).strip()
    if not s:
        return None
    try:
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


# Composite v2 optional enum fields (invalid/missing values normalize to "unknown" — safe,
# observe-only; a bad enum NEVER rejects the whole alert, it just becomes "unknown").
VWAP_STATES = ("above", "below", "reclaim", "reject", "unknown")
BB_STATES = ("squeeze", "expansion_up", "expansion_down", "normal", "unknown")
VOLUME_STATES = ("active", "dead", "spike", "unknown")
HTF_BIASES = ("bullish", "bearish", "neutral", "unknown")


# Composite v3 optional enum fields (invalid/missing -> "unknown"; never reject the alert).
ADX_STATES = ("weak_trend", "normal_trend", "strong_trend", "unknown")
SUPERTREND_DIRECTIONS = ("bullish", "bearish", "neutral", "unknown")
CANDLE_PRESSURES = ("bull_close_near_high", "bear_close_near_low", "upper_wick_rejection",
                    "lower_wick_rejection", "neutral_candle", "unknown")
RANGE_STATES = ("breakout_up", "breakout_down", "range_top", "range_bottom", "range_middle",
                "unknown")
MTF_ALIGNMENTS = ("bullish_aligned", "bearish_aligned", "mixed", "neutral", "unknown")


# ---- Order-flow / event schema (v4) optional fields (OBSERVE-ONLY; invalid -> "unknown"/None) ----
# These let real order-flow + event data be fed so the bot can GRADE whether each has an edge. They
# NEVER place/size/veto a trade (event_blackout is measured only — it does not trigger a blackout).
CVD_STATES = ("bullish", "bearish", "neutral", "divergence_bull", "divergence_bear",
              # accept the Composite v4 Pine vocabulary too (so the field isn't dropped to unknown)
              "buy_pressure", "sell_pressure", "unknown")
FUNDING_STATES = ("positive", "negative", "neutral", "extreme_positive", "extreme_negative",
                  # Composite v4 Pine crowding vocabulary
                  "long_crowded", "short_crowded", "unknown")


def _enum(value, allowed: tuple, default: str = "unknown") -> str:
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


def compact_alert_record(ev: "TradingViewSignalEvent") -> dict:
    """Compact chronological alert for LLM trend tracing (observe-only).

    Includes OHLC extras from Hermes BarClose and RSI-div fields for overlay FIFO.
    """
    row = {
        "event_id": ev.event_id,
        "received_at": round(float(ev.received_at or 0.0), 3),
        "bar_time": ev.bar_time,
        "timeframe": ev.timeframe,
        "direction": ev.direction,
        "strength": ev.strength,
        "signal_level": ev.signal_level,
        "price": ev.price,
        "indicator_name": ev.indicator_name,
        "signal_kind": getattr(ev, "signal_kind", None),
        "divergence_kind": getattr(ev, "divergence_kind", None),
        "rsi": getattr(ev, "rsi", None),
        "rsi_delta": getattr(ev, "rsi_delta", None),
        "rsi_zone": getattr(ev, "rsi_zone", None),
        "band_event": getattr(ev, "band_event", None),
        "rsi_os_threshold": getattr(ev, "rsi_os_threshold", None),
        "rsi_ob_threshold": getattr(ev, "rsi_ob_threshold", None),
        "price_delta_pct": getattr(ev, "price_delta_pct", None),
        "open": getattr(ev, "open_price", None),
        "high": getattr(ev, "high_price", None),
        "low": getattr(ev, "low_price", None),
        "close": getattr(ev, "close_price", None) if getattr(ev, "close_price", None) is not None
                 else ev.price,
        "body_pct": getattr(ev, "body_pct", None),
        "body_ratio": getattr(ev, "body_ratio", None),
        "streak": getattr(ev, "streak", None),
    }
    return {k: v for k, v in row.items() if v is not None}


def strength_bucket(x) -> str:
    if x is None:
        return "na"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "na"
    if v < 0.5:
        return "<0.5"
    if v < 0.8:
        return "0.5-0.8"
    return ">=0.8"


@dataclass
class TradingViewSignalEvent:
    """Normalized, observe-only external signal from a TradingView indicator alert."""
    event_id: str
    bot_name: str
    symbol: str
    timeframe: Optional[str]
    bar_time: Optional[float]
    received_at: float
    direction: str                       # "UP" | "DOWN" | "FLAT"
    strength: Optional[float]
    indicator_name: Optional[str]
    raw_payload_hash: str
    signal_level: Optional[str] = None   # e.g. RSI divergence level/class ("regular"/"hidden"/"3")
    price: Optional[float] = None        # price reported by the alert (observe-only reference)
    # ---- Bar-close 15m OHLC extras (Hermes_BarClose_15m; observe-only) ----
    signal_kind: Optional[str] = None    # e.g. "bar_close_5m" | "rsi_divergence"
    divergence_kind: Optional[str] = None
    rsi: Optional[float] = None
    rsi_delta: Optional[float] = None
    rsi_zone: Optional[str] = None       # oversold | neutral | overbought (rsi_band)
    band_event: Optional[str] = None     # enter_oversold | exit_oversold | enter_overbought | ...
    rsi_os_threshold: Optional[float] = None
    rsi_ob_threshold: Optional[float] = None
    price_delta_pct: Optional[float] = None
    open_price: Optional[float] = None
    high_price: Optional[float] = None
    low_price: Optional[float] = None
    close_price: Optional[float] = None
    body_pct: Optional[float] = None
    body_ratio: Optional[float] = None
    streak: Optional[int] = None
    # ---- Composite v2 optional features (observe-only) ----
    vwap_state: str = "unknown"
    bb_state: str = "unknown"
    relative_volume: Optional[float] = None
    volume_state: str = "unknown"
    htf_bias: str = "unknown"
    composite_version: Optional[str] = None
    # ---- Composite v3 optional features (observe-only) ----
    adx: Optional[float] = None
    adx_state: str = "unknown"
    supertrend_value: Optional[float] = None
    supertrend_direction: str = "unknown"
    supertrend_aligned: Optional[bool] = None
    candle_pressure: str = "unknown"
    close_position: Optional[float] = None
    upper_wick_ratio: Optional[float] = None
    lower_wick_ratio: Optional[float] = None
    range_state: str = "unknown"
    range_lookback: Optional[float] = None
    prior_range_high: Optional[float] = None
    prior_range_low: Optional[float] = None
    mtf_alignment: str = "unknown"
    bar_confirmed: Optional[bool] = None
    signal_age_ms: Optional[float] = None
    non_repainting: Optional[bool] = None
    # ---- Order-flow / event features (v4, observe-only) ----
    cvd_state: str = "unknown"
    funding_state: str = "unknown"
    liquidation_spike: Optional[bool] = None
    event_blackout: Optional[bool] = None
    source: str = "tradingview"
    observe_only: bool = True

    def _v2(self) -> dict:
        return {"vwap_state": self.vwap_state, "bb_state": self.bb_state,
                "relative_volume": self.relative_volume, "volume_state": self.volume_state,
                "htf_bias": self.htf_bias, "composite_version": self.composite_version}

    def _v3(self) -> dict:
        return {"adx": self.adx, "adx_state": self.adx_state,
                "supertrend_value": self.supertrend_value,
                "supertrend_direction": self.supertrend_direction,
                "supertrend_aligned": self.supertrend_aligned,
                "candle_pressure": self.candle_pressure, "body_ratio": self.body_ratio,
                "close_position": self.close_position, "upper_wick_ratio": self.upper_wick_ratio,
                "lower_wick_ratio": self.lower_wick_ratio, "range_state": self.range_state,
                "range_lookback": self.range_lookback, "prior_range_high": self.prior_range_high,
                "prior_range_low": self.prior_range_low, "mtf_alignment": self.mtf_alignment,
                "bar_confirmed": self.bar_confirmed, "signal_age_ms": self.signal_age_ms,
                "non_repainting": self.non_repainting}

    def _v4(self) -> dict:
        return {"cvd_state": self.cvd_state, "funding_state": self.funding_state,
                "liquidation_spike": self.liquidation_spike, "event_blackout": self.event_blackout}

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, "source": self.source, "bot_name": self.bot_name,
                "symbol": self.symbol, "timeframe": self.timeframe, "bar_time": self.bar_time,
                "received_at": round(self.received_at, 3), "direction": self.direction,
                "strength": self.strength, "signal_level": self.signal_level,
                "price": self.price, "indicator_name": self.indicator_name,
                "signal_kind": self.signal_kind,
                "divergence_kind": self.divergence_kind,
                "rsi": self.rsi,
                "rsi_delta": self.rsi_delta,
                "price_delta_pct": self.price_delta_pct,
                "open": self.open_price, "high": self.high_price, "low": self.low_price,
                "close": self.close_price, "body_pct": self.body_pct,
                "body_ratio": self.body_ratio, "streak": self.streak,
                "raw_payload_hash": self.raw_payload_hash, "observe_only": True,
                **self._v2(), **self._v3(), **self._v4()}

    def as_feature(self, *, now: Optional[float] = None) -> dict:
        """The observe-only feature view attached to a candidate (never trades/sizes/vetoes)."""
        now = float(now if now is not None else time.time())
        return {"source": "tradingview", "observe_only": True, "event_id": self.event_id,
                "direction": self.direction, "strength": self.strength,
                "strength_bucket": strength_bucket(self.strength),
                "signal_level": self.signal_level, "price": self.price,
                "indicator_name": self.indicator_name, "symbol": self.symbol,
                "timeframe": self.timeframe, "bar_time": self.bar_time,
                "age_s": (round(now - self.received_at, 3)),
                **self._v2(), **self._v3(), **self._v4()}


class TradingViewEdge:
    """OBSERVE-ONLY measurement: did the TradingView signal present at entry predict the 5-min
    Chainlink outcome, and did the bot win more when its side aligned with the signal?

    Grouped by direction / timeframe / symbol / alignment. REPORT-ONLY — it never affects which
    paper trades are taken (it is computed at SETTLEMENT, after the outcome is known)."""

    MIN_EVIDENCE = 30          # min UP/DOWN signals before claiming a directional edge

    def __init__(self):
        self.n_total = 0
        self.outcomes_up = 0
        self.n_with_signal = 0
        self.n_no_signal = 0
        self.signal_evaluated = 0      # UP/DOWN signals only (FLAT/none excluded)
        self.signal_correct = 0
        self.dims: dict = {"direction": {}, "timeframe": {}, "symbol": {}, "alignment": {},
                           "tf_confirm": {}}     # 1m+5m cross-timeframe confirmation (observe-only)

    def _b(self, dim: str, key: str) -> dict:
        return self.dims[dim].setdefault(str(key), {"n": 0, "sig_eval": 0, "sig_correct": 0,
                                                     "bot_wins": 0, "pnl": 0.0})

    def record(self, *, tv, traded_side, outcome_up: bool, won: bool, pnl: float) -> None:
        self.n_total += 1
        if outcome_up:
            self.outcomes_up += 1
        won = bool(won)
        pnl = float(pnl or 0.0)
        tv = tv or {}
        direction = tv.get("direction")
        tf = tv.get("timeframe")
        sym = tv.get("symbol")
        has_dir = direction in ("UP", "DOWN")
        if direction in ("UP", "DOWN", "FLAT"):
            self.n_with_signal += 1
        else:
            self.n_no_signal += 1
        correct = None
        if has_dir:
            correct = (direction == "UP" and outcome_up) or (direction == "DOWN" and not outcome_up)
            self.signal_evaluated += 1
            self.signal_correct += int(bool(correct))
        if has_dir and traded_side in ("up", "down"):
            aligned = ((direction == "UP" and traded_side == "up")
                       or (direction == "DOWN" and traded_side == "down"))
            align_key = "aligned" if aligned else "opposed"
        elif direction == "FLAT":
            align_key = "flat_signal"
        else:
            align_key = "no_signal"

        def bump(dim, key):
            b = self._b(dim, key)
            b["n"] += 1
            b["bot_wins"] += int(won)
            b["pnl"] = round(b["pnl"] + pnl, 6)
            if correct is not None:
                b["sig_eval"] += 1
                b["sig_correct"] += int(bool(correct))
        bump("direction", direction or "none")
        bump("timeframe", tf or "none")
        bump("symbol", sym or "none")
        bump("alignment", align_key)
        bump("tf_confirm", tv.get("tf_confirm") or "none")   # graded 1m+5m cross-confirmation

    @staticmethod
    def _bucket(b: dict) -> dict:
        return {"n": b["n"],
                "signal_hit_rate": (round(b["sig_correct"] / b["sig_eval"], 4) if b["sig_eval"]
                                    else None),
                "bot_win_rate": (round(b["bot_wins"] / b["n"], 4) if b["n"] else None),
                "pnl_usd": round(b["pnl"], 4),
                "avg_pnl_usd": (round(b["pnl"] / b["n"], 4) if b["n"] else None)}

    def report(self) -> dict:
        base_up = round(self.outcomes_up / self.n_total, 4) if self.n_total else None
        hit = (round(self.signal_correct / self.signal_evaluated, 4)
               if self.signal_evaluated else None)
        dims = {f"by_{d}": {k: self._bucket(v) for k, v in self.dims[d].items()} for d in self.dims}
        al = self.dims["alignment"]
        aligned_wr = (self._bucket(al["aligned"])["bot_win_rate"] if "aligned" in al else None)
        opposed_wr = (self._bucket(al["opposed"])["bot_win_rate"] if "opposed" in al else None)
        verdict = "insufficient_evidence"
        if self.signal_evaluated >= self.MIN_EVIDENCE and hit is not None:
            if hit >= 0.55:
                verdict = "signal_predictive_edge"
            elif hit <= 0.45:
                verdict = "signal_inverse_edge"      # consistently wrong -> a fade signal
            else:
                verdict = "no_directional_edge"
        return {
            "report_only": True, "observe_only": True,
            "min_evidence": self.MIN_EVIDENCE,
            "n_settled_with_signal": self.n_with_signal,
            "n_settled_no_signal": self.n_no_signal,
            "signal_evaluated_up_down": self.signal_evaluated,
            "signal_hit_rate": hit, "baseline_up_rate": base_up,
            "aligned_bot_win_rate": aligned_wr, "opposed_bot_win_rate": opposed_wr,
            "verdict": verdict,
            **dims,
            "note": ("observe-only: did the TradingView signal at entry predict the 5-min "
                     "Chainlink outcome (signal_hit_rate vs baseline_up_rate), and did aligning "
                     "help the bot win (aligned vs opposed bot_win_rate)? Never affects trading."),
        }

    def to_state(self) -> dict:
        return {"n_total": self.n_total, "outcomes_up": self.outcomes_up,
                "n_with_signal": self.n_with_signal, "n_no_signal": self.n_no_signal,
                "signal_evaluated": self.signal_evaluated, "signal_correct": self.signal_correct,
                "dims": {d: {k: dict(v) for k, v in self.dims[d].items()} for d in self.dims}}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.n_total = int(data.get("n_total", 0) or 0)
        self.outcomes_up = int(data.get("outcomes_up", 0) or 0)
        self.n_with_signal = int(data.get("n_with_signal", 0) or 0)
        self.n_no_signal = int(data.get("n_no_signal", 0) or 0)
        self.signal_evaluated = int(data.get("signal_evaluated", 0) or 0)
        self.signal_correct = int(data.get("signal_correct", 0) or 0)
        for d in self.dims:
            self.dims[d] = {}
            for k, v in (data.get("dims") or {}).get(d, {}).items():
                self.dims[d][k] = {"n": int(v.get("n", 0) or 0),
                                   "sig_eval": int(v.get("sig_eval", 0) or 0),
                                   "sig_correct": int(v.get("sig_correct", 0) or 0),
                                   "bot_wins": int(v.get("bot_wins", 0) or 0),
                                   "pnl": float(v.get("pnl", 0.0) or 0.0)}


class TradingViewSignalLearner:
    """OBSERVE-ONLY fast-learning layer: for each settled paper trade that carried a TradingView
    signal, bucket win-rate / PnL / EV-after-cost by every signal + market-context dimension, rank
    the best/worst RSI-divergence levels, and emit promotion DIAGNOSTICS (which buckets clear a
    high bar: win_rate >= 80%, positive EV after slippage, clean reconciliation, enough samples).

    It NEVER promotes a bucket to trading authority on its own — promotion stays config-gated and
    the execution gate remains the sole trade authority."""

    DIMS = ("direction", "signal_level", "strength_bucket", "indicator_name", "hurst_regime",
            "zscore_bucket", "ttc_bucket", "spread_bucket", "depth_bucket",
            # Composite v2 dimensions
            "vwap_state", "bb_state", "volume_state", "htf_bias", "composite_version",
            # Composite v3 dimensions
            "adx_state", "supertrend_direction", "candle_pressure", "range_state", "mtf_alignment",
            # Composite v4 order-flow / event dimensions
            "cvd_state", "funding_state", "liquidation_spike", "event_blackout")

    def __init__(self):
        self.dims: dict = {d: {} for d in self.DIMS}
        self.settled = 0
        self.accepted = 0
        self.rejected = 0
        self.accepted_by_direction: dict = {}
        self.rejected_by_direction: dict = {}

    @staticmethod
    def _stat() -> dict:
        return {"n": 0, "wins": 0, "pnl": 0.0, "ev": 0.0, "reconciled_n": 0}

    def record_candidate(self, direction: Optional[str], *, accepted: bool) -> None:
        """Count accepted vs rejected outcomes for candidates that carried a TradingView signal."""
        d = str(direction or "na")
        if accepted:
            self.accepted += 1
            self.accepted_by_direction[d] = self.accepted_by_direction.get(d, 0) + 1
        else:
            self.rejected += 1
            self.rejected_by_direction[d] = self.rejected_by_direction.get(d, 0) + 1

    def record_settled(self, tags: dict, *, won: bool, pnl: float, ev_after_cost: Optional[float],
                       reconciled: bool) -> None:
        self.settled += 1
        won = bool(won)
        pnl = float(pnl or 0.0)
        ev = float(ev_after_cost or 0.0)
        for dim in self.DIMS:
            b = tags.get(dim)
            s = self.dims[dim].setdefault(str(b if b is not None else "na"), self._stat())
            s["n"] += 1
            s["wins"] += int(won)
            s["pnl"] = round(s["pnl"] + pnl, 6)
            s["ev"] = round(s["ev"] + ev, 6)
            s["reconciled_n"] += int(bool(reconciled))

    @staticmethod
    def _b(s: dict) -> dict:
        n = s["n"]
        return {"n": n, "win_rate": (round(s["wins"] / n, 4) if n else None),
                "pnl_usd": round(s["pnl"], 4),
                "avg_ev_after_cost": (round(s["ev"] / n, 6) if n else None),
                "all_reconciled": (s["reconciled_n"] == n and n > 0)}

    def promotion_diagnostics(self, *, allowed: bool, min_samples: int,
                              min_win_rate: float = 0.8) -> dict:
        eligible = []
        for dim in self.DIMS:
            for b, s in self.dims[dim].items():
                if b == "na" or s["n"] < min_samples:
                    continue
                wr = s["wins"] / s["n"]
                ev = s["ev"] / s["n"]
                recon = (s["reconciled_n"] == s["n"])
                if wr >= min_win_rate and ev > 0 and recon:
                    eligible.append({"dimension": dim, "bucket": b, "n": s["n"],
                                     "win_rate": round(wr, 4), "avg_ev_after_cost": round(ev, 6)})
        eligible.sort(key=lambda x: (x["win_rate"], x["avg_ev_after_cost"]), reverse=True)
        return {
            "promotion_allowed_by_config": bool(allowed),
            "min_samples": min_samples, "min_win_rate": min_win_rate,
            "require_positive_ev_after_slippage": True, "require_clean_reconciliation": True,
            "eligible_buckets": eligible,
            "any_eligible": bool(eligible),
            "note": ("observe-only diagnostic; eligible buckets are NOT auto-promoted to trading "
                     "authority unless promotion_allowed_by_config is true AND explicitly wired. "
                     "The execution gate remains the sole trade authority."),
        }

    def report(self, *, promotion_allowed: bool = False, min_samples: int = 50,
               min_win_rate: float = 0.8) -> dict:
        out = {"observe_only": True, "report_only": True, "affects_trading": False,
               "settled_with_signal": self.settled,
               "accepted": self.accepted, "rejected": self.rejected,
               "accepted_by_direction": dict(self.accepted_by_direction),
               "rejected_by_direction": dict(self.rejected_by_direction)}
        for dim in self.DIMS:
            out["by_" + dim] = {b: self._b(s) for b, s in self.dims[dim].items()}
        # best/worst RSI-divergence levels (signal_level buckets ranked by win-rate, min 3 samples)
        lvl = [{"signal_level": b, **self._b(s)}
               for b, s in self.dims["signal_level"].items() if b != "na" and s["n"] >= 3]
        lvl.sort(key=lambda x: ((x["win_rate"] or 0.0), x["pnl_usd"]), reverse=True)
        out["best_signal_levels"] = lvl[:3]
        out["worst_signal_levels"] = list(reversed(lvl[-3:])) if len(lvl) > 0 else []
        # best/worst TradingView buckets across ALL dimensions (ranked by EV-after-cost, then PnL)
        ranked = []
        for dim in self.DIMS:
            for b, s in self.dims[dim].items():
                if b == "na" or s["n"] < 3:
                    continue
                ranked.append({"dimension": dim, "bucket": b, **self._b(s)})
        ranked.sort(key=lambda r: ((r["avg_ev_after_cost"] if r["avg_ev_after_cost"] is not None
                                    else -9), r["pnl_usd"]), reverse=True)
        out["best_buckets"] = ranked[:5]
        out["worst_buckets"] = list(reversed(ranked[-5:])) if ranked else []
        out["promotion"] = self.promotion_diagnostics(
            allowed=promotion_allowed, min_samples=min_samples, min_win_rate=min_win_rate)
        return out

    def to_state(self) -> dict:
        return {"dims": {d: {b: dict(s) for b, s in self.dims[d].items()} for d in self.DIMS},
                "settled": self.settled, "accepted": self.accepted, "rejected": self.rejected,
                "accepted_by_direction": dict(self.accepted_by_direction),
                "rejected_by_direction": dict(self.rejected_by_direction)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.dims = {d: {} for d in self.DIMS}
        for d in self.DIMS:
            for b, s in (data.get("dims") or {}).get(d, {}).items():
                self.dims[d][b] = {"n": int(s.get("n", 0) or 0), "wins": int(s.get("wins", 0) or 0),
                                   "pnl": float(s.get("pnl", 0.0) or 0.0),
                                   "ev": float(s.get("ev", 0.0) or 0.0),
                                   "reconciled_n": int(s.get("reconciled_n", 0) or 0)}
        self.settled = int(data.get("settled", 0) or 0)
        self.accepted = int(data.get("accepted", 0) or 0)
        self.rejected = int(data.get("rejected", 0) or 0)
        self.accepted_by_direction = {k: int(v or 0)
                                      for k, v in (data.get("accepted_by_direction") or {}).items()}
        self.rejected_by_direction = {k: int(v or 0)
                                      for k, v in (data.get("rejected_by_direction") or {}).items()}


class RSITrendModel:
    """OBSERVE-ONLY: track the per-symbol history of RSI alerts, classify the current up/down
    trend, and learn ``P(next 5-min Chainlink outcome | current RSI trend state)`` so it can
    PREDICT the next 5-min window's direction — then SCORE its own predictions against reality.

    Leakage-free: the prediction for a window is made from counts that EXCLUDE that window's own
    outcome (counts are updated only at settlement, after scoring). REPORT-ONLY — it never affects
    which paper trades are taken."""

    HIST = 64                  # alerts kept per symbol
    MIN_STATE_N = 8            # min settled samples for a trend-state before it will predict

    def __init__(self):
        self.hist: dict = {}            # symbol -> deque[(ts, direction)]
        self.state_counts: dict = {}    # symbol -> {state_key: {"up": int, "n": int}}
        self.pred_n = 0
        self.pred_correct = 0
        self.pred_by_symbol: dict = {}  # symbol -> {"n","correct"}
        # raw-signal predictiveness over ALL signals (not just traded windows): did the RSI
        # direction predict the BTC move over the horizon after the alert?
        self.sig_n = 0
        self.sig_correct = 0
        self.sig_by_direction: dict = {}   # "UP"/"DOWN" -> {"n","correct"}

    def observe(self, *, symbol: str, direction: str, ts: float) -> None:
        if not symbol:
            return
        dq = self.hist.setdefault(symbol, deque(maxlen=self.HIST))
        dq.append((float(ts or 0.0), direction))

    @staticmethod
    def _streak(dq) -> "tuple[int, Optional[str]]":
        """Signed run length of the latest consecutive same non-FLAT direction (UP=+, DOWN=-)."""
        if not dq:
            return 0, None
        last = dq[-1][1]
        if last not in ("UP", "DOWN"):
            return 0, last
        run = 0
        for _, d in reversed(dq):
            if d == last:
                run += 1
            else:
                break
        return (run if last == "UP" else -run), last

    def _state_key(self, dq) -> str:
        streak, last = self._streak(dq)
        if last not in ("UP", "DOWN"):
            return "flat_or_none"
        return ("up" if streak > 0 else "down") + "_streak" + str(min(abs(streak), 3))

    def trend(self, symbol: str) -> dict:
        dq = self.hist.get(symbol)
        if not dq:
            return {"symbol": symbol, "n": 0, "last_direction": None, "streak": 0,
                    "state": "flat_or_none", "recent_up_fraction": None}
        streak, last = self._streak(dq)
        recent = [d for _, d in list(dq)[-8:] if d in ("UP", "DOWN")]
        ups = sum(1 for d in recent if d == "UP")
        return {"symbol": symbol, "n": len(dq), "last_direction": last, "streak": streak,
                "state": self._state_key(dq),
                "recent_up_fraction": (round(ups / len(recent), 3) if recent else None)}

    def predict(self, symbol: str) -> dict:
        """Observe-only next-5-min prediction from P(up | current RSI trend state)."""
        dq = self.hist.get(symbol)
        if not dq:
            return {"symbol": symbol, "prediction": None, "reason": "no_history"}
        state = self._state_key(dq)
        c = (self.state_counts.get(symbol) or {}).get(state)
        if not c or c["n"] < self.MIN_STATE_N:
            return {"symbol": symbol, "state": state, "prediction": None, "prob_up": None,
                    "reason": "insufficient_state_samples", "state_n": (c["n"] if c else 0)}
        p_up = c["up"] / c["n"]
        return {"symbol": symbol, "state": state,
                "prediction": ("UP" if p_up > 0.5 else "DOWN"), "prob_up": round(p_up, 4),
                "confidence": round(abs(p_up - 0.5) * 2, 3), "state_n": c["n"],
                "basis": "conditional_outcome_given_rsi_trend"}

    def score_and_update(self, *, symbol: str, state: Optional[str], predicted: Optional[str],
                         outcome_up: bool) -> None:
        """Score the entry-time prediction (leakage-free), then fold the realized outcome into the
        conditional distribution for that trend state."""
        if predicted in ("UP", "DOWN"):
            correct = (predicted == "UP" and outcome_up) or (predicted == "DOWN" and not outcome_up)
            self.pred_n += 1
            self.pred_correct += int(bool(correct))
            ps = self.pred_by_symbol.setdefault(symbol or "none", {"n": 0, "correct": 0})
            ps["n"] += 1
            ps["correct"] += int(bool(correct))
        if state:
            sc = self.state_counts.setdefault(symbol or "none", {}).setdefault(
                state, {"up": 0, "n": 0})
            sc["n"] += 1
            sc["up"] += int(bool(outcome_up))

    def record_signal_outcome(self, *, symbol: str, state: Optional[str], model_pred: Optional[str],
                              signal_direction: Optional[str], outcome_up: bool) -> None:
        """Learn from EVERY TradingView signal's realized forward BTC move (traded or not): score
        the raw RSI direction's predictiveness, score the model's leakage-free prediction, and
        fold the outcome into the conditional P(up | trend state)."""
        if signal_direction in ("UP", "DOWN"):
            correct = (signal_direction == "UP") == bool(outcome_up)
            self.sig_n += 1
            self.sig_correct += int(correct)
            b = self.sig_by_direction.setdefault(signal_direction, {"n": 0, "correct": 0})
            b["n"] += 1
            b["correct"] += int(correct)
        self.score_and_update(symbol=symbol, state=state, predicted=model_pred,
                              outcome_up=outcome_up)

    def report(self) -> dict:
        acc = round(self.pred_correct / self.pred_n, 4) if self.pred_n else None
        sig_hit = round(self.sig_correct / self.sig_n, 4) if self.sig_n else None
        return {
            "observe_only": True, "report_only": True,
            "min_state_samples": self.MIN_STATE_N,
            "signals_evaluated": self.sig_n,
            "signal_direction_hit_rate": sig_hit,
            "signal_hit_rate_by_direction": {
                k: {"n": v["n"], "hit_rate": (round(v["correct"] / v["n"], 4) if v["n"] else None)}
                for k, v in self.sig_by_direction.items()},
            "predictions_scored": self.pred_n,
            "prediction_accuracy": acc,
            "prediction_accuracy_by_symbol": {
                s: {"n": v["n"], "accuracy": (round(v["correct"] / v["n"], 4) if v["n"] else None)}
                for s, v in self.pred_by_symbol.items()},
            "current_trend": {s: self.trend(s) for s in self.hist},
            "next_window_prediction": {s: self.predict(s) for s in self.hist},
            "learned_states": {s: {k: {"n": v["n"],
                                       "p_up": (round(v["up"] / v["n"], 4) if v["n"] else None)}
                                   for k, v in sc.items()}
                               for s, sc in self.state_counts.items()},
            "note": ("observe-only: learns P(next 5-min outcome | RSI alert trend state) from the "
                     "alert history and scores its own next-window predictions. Never trades."),
        }

    def to_state(self) -> dict:
        return {"hist": {s: [[t, d] for t, d in dq] for s, dq in self.hist.items()},
                "state_counts": {s: {k: dict(v) for k, v in sc.items()}
                                 for s, sc in self.state_counts.items()},
                "pred_n": self.pred_n, "pred_correct": self.pred_correct,
                "pred_by_symbol": {s: dict(v) for s, v in self.pred_by_symbol.items()},
                "sig_n": self.sig_n, "sig_correct": self.sig_correct,
                "sig_by_direction": {k: dict(v) for k, v in self.sig_by_direction.items()}}

    def canonicalize_storage(self, feature_symbol: str = "BTCUSD") -> None:
        """Merge legacy per-ticker RSI history (e.g. BTCUSD test alerts) into feature_symbol."""
        feat = canonical_storage_symbol(feature_symbol, feature_symbol)

        def _canon(sym: Optional[str]) -> str:
            return canonical_storage_symbol(sym, feat)

        merged_hist: dict = {}
        for sym, dq in self.hist.items():
            canon = _canon(sym)
            seq = list(merged_hist.get(canon, [])) + list(dq)
            seq.sort(key=lambda x: float(x[0] or 0.0))
            merged_hist[canon] = deque(seq[-self.HIST:], maxlen=self.HIST)
        self.hist = merged_hist

        merged_sc: dict = {}
        for sym, states in self.state_counts.items():
            canon = _canon(sym)
            dst = merged_sc.setdefault(canon, {})
            for state, c in states.items():
                b = dst.setdefault(state, {"up": 0, "n": 0})
                b["up"] += int(c.get("up", 0) or 0)
                b["n"] += int(c.get("n", 0) or 0)
        self.state_counts = merged_sc

        merged_pred: dict = {}
        for sym, v in self.pred_by_symbol.items():
            canon = _canon(sym)
            b = merged_pred.setdefault(canon, {"n": 0, "correct": 0})
            b["n"] += int(v.get("n", 0) or 0)
            b["correct"] += int(v.get("correct", 0) or 0)
        self.pred_by_symbol = merged_pred

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.hist = {}
        for s, seq in (data.get("hist") or {}).items():
            dq = deque(maxlen=self.HIST)
            for item in seq:
                try:
                    dq.append((float(item[0]), item[1]))
                except Exception:  # noqa: BLE001
                    continue
            self.hist[s] = dq
        self.state_counts = {}
        for s, sc in (data.get("state_counts") or {}).items():
            self.state_counts[s] = {k: {"up": int(v.get("up", 0) or 0), "n": int(v.get("n", 0) or 0)}
                                    for k, v in sc.items()}
        self.pred_n = int(data.get("pred_n", 0) or 0)
        self.pred_correct = int(data.get("pred_correct", 0) or 0)
        self.pred_by_symbol = {s: {"n": int(v.get("n", 0) or 0), "correct": int(v.get("correct", 0) or 0)}
                               for s, v in (data.get("pred_by_symbol") or {}).items()}
        self.sig_n = int(data.get("sig_n", 0) or 0)
        self.sig_correct = int(data.get("sig_correct", 0) or 0)
        self.sig_by_direction = {k: {"n": int(v.get("n", 0) or 0), "correct": int(v.get("correct", 0) or 0)}
                                 for k, v in (data.get("sig_by_direction") or {}).items()}


def _event_from_dict(d) -> Optional["TradingViewSignalEvent"]:
    if not isinstance(d, dict) or not d.get("event_id"):
        return None
    try:
        streak_raw = d.get("streak")
        try:
            streak_val = int(streak_raw) if streak_raw is not None else None
        except (TypeError, ValueError):
            streak_val = None
        return TradingViewSignalEvent(
            event_id=str(d["event_id"]), bot_name=str(d.get("bot_name") or ""),
            symbol=str(d.get("symbol") or ""), timeframe=d.get("timeframe"),
            bar_time=d.get("bar_time"), received_at=float(d.get("received_at") or 0.0),
            direction=str(d.get("direction") or "FLAT"), strength=d.get("strength"),
            signal_level=d.get("signal_level"), price=d.get("price"),
            signal_kind=(str(d.get("signal_kind") or "").strip() or None),
            divergence_kind=(str(d.get("divergence_kind") or "").strip() or None),
            rsi=_as_float(d.get("rsi")),
            rsi_delta=_as_float(d.get("rsi_delta")),
            rsi_zone=(str(d.get("rsi_zone") or "").strip().lower() or None),
            band_event=(str(d.get("band_event") or "").strip().lower() or None),
            rsi_os_threshold=_as_float(d.get("rsi_os_threshold")),
            rsi_ob_threshold=_as_float(d.get("rsi_ob_threshold")),
            price_delta_pct=_as_float(d.get("price_delta_pct")),
            open_price=_as_float(d.get("open") if d.get("open") is not None else d.get("open_price")),
            high_price=_as_float(d.get("high") if d.get("high") is not None else d.get("high_price")),
            low_price=_as_float(d.get("low") if d.get("low") is not None else d.get("low_price")),
            close_price=_as_float(d.get("close") if d.get("close") is not None else d.get("close_price")),
            body_pct=_as_float(d.get("body_pct")),
            body_ratio=_as_float(d.get("body_ratio")),
            streak=streak_val,
            vwap_state=_enum(d.get("vwap_state"), VWAP_STATES),
            bb_state=_enum(d.get("bb_state"), BB_STATES),
            relative_volume=d.get("relative_volume"),
            volume_state=_enum(d.get("volume_state"), VOLUME_STATES),
            htf_bias=_enum(d.get("htf_bias"), HTF_BIASES),
            composite_version=d.get("composite_version"),
            adx=_as_float(d.get("adx")), adx_state=_enum(d.get("adx_state"), ADX_STATES),
            supertrend_value=_as_float(d.get("supertrend_value")),
            supertrend_direction=_enum(d.get("supertrend_direction"), SUPERTREND_DIRECTIONS),
            supertrend_aligned=_as_bool(d.get("supertrend_aligned")),
            candle_pressure=_enum(d.get("candle_pressure"), CANDLE_PRESSURES),
            close_position=_as_float(d.get("close_position")),
            upper_wick_ratio=_as_float(d.get("upper_wick_ratio")),
            lower_wick_ratio=_as_float(d.get("lower_wick_ratio")),
            range_state=_enum(d.get("range_state"), RANGE_STATES),
            range_lookback=_as_float(d.get("range_lookback")),
            prior_range_high=_as_float(d.get("prior_range_high")),
            prior_range_low=_as_float(d.get("prior_range_low")),
            mtf_alignment=_enum(d.get("mtf_alignment"), MTF_ALIGNMENTS),
            bar_confirmed=_as_bool(d.get("bar_confirmed")),
            signal_age_ms=_as_float(d.get("signal_age_ms")),
            non_repainting=_as_bool(d.get("non_repainting")),
            cvd_state=_enum(d.get("cvd_state"), CVD_STATES),
            funding_state=_enum(d.get("funding_state"), FUNDING_STATES),
            liquidation_spike=_as_bool(d.get("liquidation_spike")),
            event_blackout=_as_bool(d.get("event_blackout")),
            indicator_name=d.get("indicator_name"),
            raw_payload_hash=str(d.get("raw_payload_hash") or ""))
    except Exception:  # noqa: BLE001
        return None


class TradingViewIntake:
    """Validates + normalizes + de-duplicates TradingView alerts and exposes report counters.

    Thread-safe: the webhook thread calls :meth:`ingest`; the engine thread calls
    :meth:`drain_pending`, :meth:`latest_feature`, and :meth:`report`."""

    def __init__(self, *, secret: str, allowed_symbols, bot_name: str = "hermes",
                 max_age_s: float = 90.0, future_skew_s: float = 30.0,
                 data_dir: Optional[str] = None, dedupe_capacity: int = 5000,
                 header_name: str = "X-Tradingview-Secret",
                 feature_symbol: str = "BTCUSD",
                 expected_event_id_suffix: str = "",
                 mtf_timeframes: Optional[tuple[str, ...]] = None,
                 confirm_windows_by_tf: Optional[dict[str, float]] = None,
                 confirm_window_s: float = 360.0,
                 confirm_window_10m_s: float = 660.0,
                 confirm_window_15m_s: float = 960.0,
                 drop_timeframes: Optional[Iterable] = None,
                 allowed_bot_names: Optional[Iterable] = None,
                 alert_history_per_symbol: int = 10,
                 rsi_div_history_per_symbol: int = 20,
                 rsi_band_history_per_symbol: int = 50):
        self.secret = str(secret or "")
        # Chart timeframes the operator retired: never tracked per-TF (no council member, no dashboard
        # row, stripped from persisted snapshots). Alerts still accepted/counted (observe-only).
        self.drop_timeframes = frozenset(
            str(t).strip() for t in (drop_timeframes or ()) if str(t).strip())
        # Chart symbol the operator feeds (INDEX:BTCUSD -> BTCUSD). Used for 5m/10m/15m
        # cross-confirmation lookups — distinct from the Chainlink oracle slug (btc/usd).
        self.feature_symbol = normalize_symbol(feature_symbol) or "BTCUSD"
        self.mtf_timeframes = tuple(mtf_timeframes) if mtf_timeframes else DEFAULT_MTF_TIMEFRAMES
        self.confirm_windows_by_tf = confirm_windows_by_tf or build_mtf_confirm_windows(
            self.mtf_timeframes,
            legacy_5m_s=confirm_window_s,
            legacy_10m_s=confirm_window_10m_s,
            legacy_15m_s=confirm_window_15m_s,
        )
        # normalize allow-list entries the same way incoming symbols are normalized, so
        # exchange-prefixed aliases (INDEX:BTCUSD) match their base symbol.
        self.allowed_symbols = {normalize_symbol(s) for s in (allowed_symbols or []) if str(s).strip()}
        self.bot_name = str(bot_name or "").strip().lower()
        # bot-name allow-list: accept alerts whose bot_name is in this set (defaults to {bot_name}).
        # Lets the operator route an alert configured with a different bot_name to this bot.
        self.allowed_bot_names = {str(b).strip().lower()
                                  for b in (allowed_bot_names or [self.bot_name])
                                  if str(b).strip()}
        self.expected_event_id_suffix = str(expected_event_id_suffix or "").strip().lower()
        self.max_age_s = float(max_age_s)
        self.future_skew_s = float(future_skew_s)
        self.header_name = header_name
        self._lock = threading.Lock()
        self._seen: "deque[str]" = deque(maxlen=int(dedupe_capacity))
        self._seen_set: set = set()
        self._pending: list = []
        self.received = 0
        self.valid = 0
        self.rejected = 0
        self.consumed = 0
        self.reject_reasons: dict = {}
        self._last_received_at: float = 0.0
        self._last_valid_at: float = 0.0
        self._last_reject_reason: Optional[str] = None
        self.latest: Optional[TradingViewSignalEvent] = None
        # per-source tracking (INDEX:BTCUSD alerts stored under feature_symbol)
        self.latest_by_symbol: dict = {}
        self.valid_by_symbol: dict = {}
        # per-(symbol,timeframe) latest so multiple alert timeframes (e.g. 4m + 5m) can be
        # CROSS-CONFIRMED instead of overwriting each other. value = (event, received_ts).
        self.latest_by_tf: dict = {}
        # Rolling per-symbol alert history for LLM trend tracing (observe-only).
        # Path FIFO = bar-close only; RSI overlay FIFO is separate (never mixed).
        self.alert_history_per_symbol = max(1, int(alert_history_per_symbol or 10))
        self.alert_history_by_symbol: dict[str, deque] = {}
        self.rsi_div_history_per_symbol = max(1, int(rsi_div_history_per_symbol or 20))
        self.rsi_div_history_by_symbol: dict[str, deque] = {}
        self.rsi_band_history_per_symbol = max(1, int(rsi_band_history_per_symbol or 50))
        self.rsi_band_history_by_symbol: dict[str, deque] = {}
        self._on_accepted: list = []
        # Legacy aliases — 5m/10m/15m windows (see confirm_windows_by_tf for full map).
        self.confirm_window_s: float = float(self.confirm_windows_by_tf.get("5", confirm_window_s))
        self.confirm_window_10m_s: float = float(
            self.confirm_windows_by_tf.get("10", confirm_window_10m_s))
        self.confirm_window_15m_s: float = float(
            self.confirm_windows_by_tf.get("15", confirm_window_15m_s))
        self._path = (Path(data_dir) / "btc_pulse_tradingview.json") if data_dir else None
        self._load_state()

    def _storage_symbol(self, symbol: Optional[str]) -> str:
        """Canonical key for counters/latest maps — all BTC-family tickers -> feature_symbol."""
        return canonical_storage_symbol(symbol, self.feature_symbol)

    def register_on_accepted(self, callback) -> None:
        """Register ``callback(ev, now=...)`` fired immediately after each valid alert."""
        self._on_accepted.append(callback)

    def _alert_history_deque(self, symbol: str) -> deque:
        store = self._storage_symbol(symbol)
        dq = self.alert_history_by_symbol.get(store)
        if dq is None:
            dq = deque(maxlen=self.alert_history_per_symbol)
            self.alert_history_by_symbol[store] = dq
        return dq

    def _rsi_div_history_deque(self, symbol: str) -> deque:
        store = self._storage_symbol(symbol)
        dq = self.rsi_div_history_by_symbol.get(store)
        if dq is None:
            dq = deque(maxlen=self.rsi_div_history_per_symbol)
            self.rsi_div_history_by_symbol[store] = dq
        return dq

    def _rsi_band_history_deque(self, symbol: str) -> deque:
        store = self._storage_symbol(symbol)
        dq = self.rsi_band_history_by_symbol.get(store)
        if dq is None:
            dq = deque(maxlen=self.rsi_band_history_per_symbol)
            self.rsi_band_history_by_symbol[store] = dq
        return dq

    @staticmethod
    def _is_rsi_divergence_ev(ev: "TradingViewSignalEvent") -> bool:
        kind = str(getattr(ev, "signal_kind", None) or "").strip().lower()
        level = str(getattr(ev, "signal_level", None) or "").strip().upper()
        return kind == "rsi_divergence" or "DIV" in level

    @staticmethod
    def _is_rsi_band_ev(ev: "TradingViewSignalEvent") -> bool:
        kind = str(getattr(ev, "signal_kind", None) or "").strip().lower()
        return kind == "rsi_band"

    @staticmethod
    def _is_bar_close_ev(ev: "TradingViewSignalEvent") -> bool:
        kind = str(getattr(ev, "signal_kind", None) or "").strip().lower()
        level = str(getattr(ev, "signal_level", None) or "").strip().upper()
        return kind.startswith("bar_close") or level in ("BAR_BULL", "BAR_BEAR")

    def _record_alert_history(self, ev: TradingViewSignalEvent) -> None:
        row = compact_alert_record(ev)
        if self._is_rsi_divergence_ev(ev):
            self._rsi_div_history_deque(ev.symbol).append(row)
            return
        if self._is_rsi_band_ev(ev):
            self._rsi_band_history_deque(ev.symbol).append(row)
            return
        # Path FIFO: prefer bar-close; still accept other non-RSI for legacy until 5m fills.
        if self._is_bar_close_ev(ev) or not self._is_rsi_divergence_ev(ev):
            self._alert_history_deque(ev.symbol).append(row)

    def alert_history_for_symbol(self, symbol: Optional[str] = None) -> list:
        """Last N path alerts for one symbol, oldest→newest (empty if unknown)."""
        sym = self._storage_symbol(symbol) if symbol else self.feature_symbol
        dq = self.alert_history_by_symbol.get(sym)
        return list(dq) if dq else []

    def rsi_div_history_for_symbol(self, symbol: Optional[str] = None) -> list:
        """Last N RSI-divergence overlay alerts, oldest→newest."""
        sym = self._storage_symbol(symbol) if symbol else self.feature_symbol
        dq = self.rsi_div_history_by_symbol.get(sym)
        return list(dq) if dq else []

    def rsi_band_history_for_symbol(self, symbol: Optional[str] = None) -> list:
        """Last N RSI 30/70 band heartbeats, oldest→newest."""
        sym = self._storage_symbol(symbol) if symbol else self.feature_symbol
        dq = self.rsi_band_history_by_symbol.get(sym)
        return list(dq) if dq else []

    def alert_history_snapshot(self, *, focus_symbol: Optional[str] = None) -> dict:
        """Per-symbol last-N alert lists for LLM trend tracing (observe-only)."""
        focus = self._storage_symbol(focus_symbol) if focus_symbol else self.feature_symbol
        by_symbol: dict = {}
        for sym in sorted(self.alert_history_by_symbol.keys()):
            alerts = self.alert_history_for_symbol(sym)
            if alerts:
                by_symbol[sym] = alerts
        rsi_by: dict = {}
        for sym in sorted(self.rsi_div_history_by_symbol.keys()):
            rows = self.rsi_div_history_for_symbol(sym)
            if rows:
                rsi_by[sym] = rows
        band_by: dict = {}
        for sym in sorted(self.rsi_band_history_by_symbol.keys()):
            rows = self.rsi_band_history_for_symbol(sym)
            if rows:
                band_by[sym] = rows
        return {
            "per_symbol_limit": self.alert_history_per_symbol,
            "rsi_div_per_symbol_limit": self.rsi_div_history_per_symbol,
            "rsi_band_per_symbol_limit": self.rsi_band_history_per_symbol,
            "focus_symbol": focus,
            "by_symbol": by_symbol,
            "rsi_divergence_by_symbol": rsi_by,
            "rsi_band_by_symbol": band_by,
        }

    def _symbol_allowed(self, symbol: str) -> bool:
        """True if symbol is allow-listed or maps to the configured BTC feature symbol."""
        if not self.allowed_symbols:
            return True
        if symbol in self.allowed_symbols:
            return True
        store = canonical_storage_symbol(symbol, self.feature_symbol)
        if store in self.allowed_symbols or store == self.feature_symbol:
            return True
        return symbol in _BTC_SYMBOL_ALIASES

    def _mtf_symbol(self, symbol: Optional[str] = None) -> Optional[str]:
        """Resolve which TV symbol key to use for MTF confirmation lookups."""
        if symbol:
            return self._storage_symbol(symbol)
        return self.feature_symbol or (self.latest.symbol if self.latest else None)

    def _canonicalize_storage(self) -> None:
        """Merge legacy per-ticker keys (e.g. BTCUSD test alerts) into feature_symbol."""
        if not self.latest_by_symbol and not self.valid_by_symbol and not self.latest_by_tf:
            return
        merged_latest: dict = {}
        for sym, ev in self.latest_by_symbol.items():
            canon = self._storage_symbol(sym)
            prev = merged_latest.get(canon)
            if prev is None or float(ev.received_at or 0) >= float(prev.received_at or 0):
                merged_latest[canon] = ev
        self.latest_by_symbol = merged_latest

        merged_valid: dict = {}
        for sym, n in self.valid_by_symbol.items():
            canon = self._storage_symbol(sym)
            merged_valid[canon] = merged_valid.get(canon, 0) + int(n or 0)
        self.valid_by_symbol = merged_valid

        merged_tf: dict = {}
        for (sym, tf), pair in self.latest_by_tf.items():
            canon = self._storage_symbol(sym)
            key = (canon, tf)
            prev = merged_tf.get(key)
            if prev is None or float(pair[1]) >= float(prev[1]):
                merged_tf[key] = pair
        _drop = LEGACY_MTF_TFS | self.drop_timeframes
        _active = frozenset(str(t) for t in self.mtf_timeframes)
        self.latest_by_tf = {
            k: v for k, v in merged_tf.items()
            if str(k[1]) not in _drop and str(k[1]) in _active
        }

        if self.latest is not None:
            canon = self._storage_symbol(self.latest.symbol)
            cur = self.latest_by_symbol.get(canon)
            if cur is None or float(self.latest.received_at or 0) >= float(cur.received_at or 0):
                self.latest_by_symbol[canon] = self.latest

    @staticmethod
    def _timeframe_from_event_id(event_id: str) -> Optional[str]:
        """Parse minute TF from ``SYMBOL-TF-ts-SIGNAL-...`` event ids."""
        parts = str(event_id or "").split("-")
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[1]
        return None

    def _is_retired_timeframe(self, tf: Optional[str]) -> bool:
        return str(tf or "") in RETIRED_MTF_TFS

    def _purge_retired_timeframe_data(self) -> bool:
        """Drop all 55m alerts from memory + counters."""
        changed = False
        removed_valid = 0

        new_seen: deque = deque(maxlen=self._seen.maxlen)
        new_set: set[str] = set()
        for eid in self._seen:
            if self._is_retired_timeframe(self._timeframe_from_event_id(eid)):
                changed = True
                continue
            new_seen.append(eid)
            new_set.add(eid)
        if len(new_seen) != len(self._seen):
            self._seen = new_seen
            self._seen_set = new_set
            changed = True

        new_history: dict[str, deque] = {}
        for sym, dq in list(self.alert_history_by_symbol.items()):
            kept = [r for r in dq if not self._is_retired_timeframe(r.get("timeframe"))]
            removed_valid += len(dq) - len(kept)
            if not kept:
                if dq:
                    changed = True
                continue
            if len(kept) != len(dq):
                changed = True
            ndq: deque = deque(maxlen=self.alert_history_per_symbol)
            for row in kept:
                ndq.append(row)
            new_history[sym] = ndq
        if new_history != self.alert_history_by_symbol:
            self.alert_history_by_symbol = new_history
            changed = True

        if self.latest is not None and self._is_retired_timeframe(self.latest.timeframe):
            self.latest = None
            changed = True

        for sym, ev in list(self.latest_by_symbol.items()):
            if self._is_retired_timeframe(ev.timeframe):
                del self.latest_by_symbol[sym]
                changed = True

        before_tf = len(self.latest_by_tf)
        self.latest_by_tf = {
            k: v for k, v in self.latest_by_tf.items()
            if not self._is_retired_timeframe(k[1])
        }
        if len(self.latest_by_tf) != before_tf:
            changed = True

        if removed_valid > 0:
            self.valid = max(0, int(self.valid) - removed_valid)
            self.received = max(int(self.received) - removed_valid, self.valid)
            self.valid_by_symbol = {
                sym: len(dq) for sym, dq in self.alert_history_by_symbol.items()
            }
            changed = True

        if self.latest is None and self.latest_by_symbol:
            self.latest = max(
                self.latest_by_symbol.values(),
                key=lambda e: float(e.received_at or 0),
            )
            changed = True

        return changed

    def _scrub_legacy_reject_stats(self) -> bool:
        """Remove lifetime unsupported_symbol rejects from old 5/10/15m chart noise."""
        changed = False
        n = int(self.reject_reasons.pop(UNSUPPORTED_SYMBOL, 0) or 0)
        if n:
            self.rejected = max(0, int(self.rejected) - n)
            changed = True
        if self._last_reject_reason == UNSUPPORTED_SYMBOL:
            self._last_reject_reason = None
            changed = True
        return changed

    # -- validation (pure given inputs) ------------------------------------- #
    def _check_secret(self, payload: dict, provided_header: Optional[str]) -> Optional[str]:
        provided = provided_header if provided_header else payload.get("secret")
        if provided is None or str(provided) == "":
            return MISSING_SECRET
        if not hmac.compare_digest(str(provided), self.secret):
            return BAD_SECRET
        return None

    def _check_event_id_suffix(self, event_id: str) -> Optional[str]:
        """Reject dual-bot Pine alerts routed to the wrong VPS (e.g. -bot2 on Bot 1)."""
        mine = self.expected_event_id_suffix
        if not mine:
            return None
        eid = str(event_id or "").strip().lower()
        if not eid:
            return None
        paired = {"bot1": "bot2", "bot2": "bot1"}.get(mine)
        if paired and eid.endswith("-" + paired):
            return WRONG_EVENT_SUFFIX
        return None

    def normalize(self, raw_bytes: bytes, *, provided_header: Optional[str], now: float):
        """Return (event, reject_reason). Exactly one is non-None."""
        raw_hash = hashlib.sha256(raw_bytes if isinstance(raw_bytes, bytes)
                                  else str(raw_bytes).encode("utf-8")).hexdigest()
        try:
            payload = json.loads(raw_bytes)
        except Exception:  # noqa: BLE001
            return None, INVALID_JSON
        if not isinstance(payload, dict):
            return None, NOT_OBJECT
        # 1) authenticate FIRST (don't leak symbol/bot validity to unauthenticated callers)
        sec = self._check_secret(payload, provided_header)
        if sec is not None:
            return None, sec
        # 2) bot name filter (allow-list; e.g. accept "hermes" + any operator-added names)
        bot = str(payload.get("bot_name") or payload.get("bot") or "").strip()
        if self.allowed_bot_names and bot.lower() not in self.allowed_bot_names:
            logger.info("tradingview alert REJECTED wrong_bot_name: got=%r allowed=%s",
                        bot, sorted(self.allowed_bot_names))
            return None, WRONG_BOT
        # 3) symbol allow-list (exchange-prefix tolerant; BTC index family auto-maps)
        symbol = normalize_symbol(payload.get("symbol") or payload.get("ticker"))
        if not symbol or not self._symbol_allowed(symbol):
            return None, UNSUPPORTED_SYMBOL
        # 4) direction
        direction = normalize_direction(payload.get("direction") or payload.get("action")
                                        or payload.get("signal"))
        if direction is None:
            return None, MALFORMED_DIRECTION
        # 5) freshness (only when a bar/alert timestamp is supplied)
        bar_time = _parse_ts(payload.get("bar_time") or payload.get("time")
                             or payload.get("timestamp"))
        if bar_time is not None:
            if (now - bar_time) > self.max_age_s or (bar_time - now) > self.future_skew_s:
                return None, STALE_TIMESTAMP
        # strength (optional)
        strength = None
        try:
            if payload.get("strength") is not None:
                strength = float(payload.get("strength"))
        except (TypeError, ValueError):
            strength = None
        event_id = str(payload.get("event_id") or payload.get("id") or "").strip() or raw_hash[:24]
        suffix_err = self._check_event_id_suffix(event_id)
        if suffix_err is not None:
            return None, suffix_err
        price = None
        try:
            if payload.get("price") is not None or payload.get("close") is not None:
                price = float(payload.get("price") if payload.get("price") is not None
                              else payload.get("close"))
        except (TypeError, ValueError):
            price = None
        signal_level = str(payload.get("signal_level") or payload.get("level")
                           or payload.get("divergence_level") or "").strip() or None
        signal_kind = str(payload.get("signal_kind") or "").strip() or None
        divergence_kind = str(payload.get("divergence_kind") or "").strip() or None
        rsi_val = _as_float(payload.get("rsi"))
        rsi_delta = _as_float(payload.get("rsi_delta"))
        rsi_zone = str(payload.get("rsi_zone") or "").strip().lower() or None
        band_event = str(payload.get("band_event") or "").strip().lower() or None
        rsi_os_threshold = _as_float(payload.get("rsi_os_threshold"))
        rsi_ob_threshold = _as_float(payload.get("rsi_ob_threshold"))
        price_delta_pct = _as_float(payload.get("price_delta_pct"))
        open_price = _as_float(payload.get("open"))
        high_price = _as_float(payload.get("high"))
        low_price = _as_float(payload.get("low"))
        close_price = _as_float(payload.get("close"))
        body_pct = _as_float(payload.get("body_pct"))
        body_ratio = _as_float(payload.get("body_ratio"))
        streak_val = None
        try:
            if payload.get("streak") is not None:
                streak_val = int(payload.get("streak"))
        except (TypeError, ValueError):
            streak_val = None
        rel_vol = None
        try:
            if payload.get("relative_volume") is not None:
                rel_vol = float(payload.get("relative_volume"))
        except (TypeError, ValueError):
            rel_vol = None
        ev = TradingViewSignalEvent(
            event_id=event_id, bot_name=(bot or self.bot_name), symbol=symbol,
            timeframe=normalize_timeframe(payload.get("timeframe") or payload.get("interval")),
            bar_time=bar_time, received_at=now, direction=direction, strength=strength,
            signal_level=signal_level, price=price,
            signal_kind=signal_kind,
            divergence_kind=divergence_kind,
            rsi=rsi_val, rsi_delta=rsi_delta,
            rsi_zone=rsi_zone, band_event=band_event,
            rsi_os_threshold=rsi_os_threshold, rsi_ob_threshold=rsi_ob_threshold,
            price_delta_pct=price_delta_pct,
            open_price=open_price, high_price=high_price, low_price=low_price,
            close_price=close_price, body_pct=body_pct, body_ratio=body_ratio,
            streak=streak_val,
            indicator_name=(str(payload.get("indicator_name") or payload.get("indicator")
                                or "").strip() or None),
            # Composite v2: invalid enums coerce to "unknown" (never reject the whole alert)
            vwap_state=_enum(payload.get("vwap_state"), VWAP_STATES),
            bb_state=_enum(payload.get("bb_state"), BB_STATES),
            relative_volume=rel_vol,
            volume_state=_enum(payload.get("volume_state"), VOLUME_STATES),
            htf_bias=_enum(payload.get("htf_bias"), HTF_BIASES),
            composite_version=(str(payload.get("composite_version") or "").strip() or None),
            # Composite v3 (invalid enums coerce to "unknown"; numerics/bools -> None if absent/bad)
            adx=_as_float(payload.get("adx")),
            adx_state=_enum(payload.get("adx_state"), ADX_STATES),
            supertrend_value=_as_float(payload.get("supertrend_value")),
            supertrend_direction=_enum(payload.get("supertrend_direction"), SUPERTREND_DIRECTIONS),
            supertrend_aligned=_as_bool(payload.get("supertrend_aligned")),
            candle_pressure=_enum(payload.get("candle_pressure"), CANDLE_PRESSURES),
            close_position=_as_float(payload.get("close_position")),
            upper_wick_ratio=_as_float(payload.get("upper_wick_ratio")),
            lower_wick_ratio=_as_float(payload.get("lower_wick_ratio")),
            range_state=_enum(payload.get("range_state"), RANGE_STATES),
            range_lookback=_as_float(payload.get("range_lookback")),
            prior_range_high=_as_float(payload.get("prior_range_high")),
            prior_range_low=_as_float(payload.get("prior_range_low")),
            mtf_alignment=_enum(payload.get("mtf_alignment"), MTF_ALIGNMENTS),
            bar_confirmed=_as_bool(payload.get("bar_confirmed")),
            signal_age_ms=_as_float(payload.get("signal_age_ms")),
            non_repainting=_as_bool(payload.get("non_repainting")),
            # Composite v4 order-flow / event (observe-only; invalid enums -> "unknown")
            cvd_state=_enum(payload.get("cvd_state"), CVD_STATES),
            funding_state=_enum(payload.get("funding_state"), FUNDING_STATES),
            liquidation_spike=_as_bool(payload.get("liquidation_spike")),
            event_blackout=_as_bool(payload.get("event_blackout")),
            raw_payload_hash=raw_hash)
        return ev, None

    # -- ingest (called by the webhook thread) ------------------------------ #
    def ingest(self, raw_bytes: bytes, *, provided_header: Optional[str] = None,
               now: Optional[float] = None):
        """Validate + record one alert. Returns (status_code:int, body:dict)."""
        now = float(now if now is not None else time.time())
        with self._lock:
            self.received += 1
            self._last_received_at = now
            ev, reason = self.normalize(raw_bytes, provided_header=provided_header, now=now)
            if reason is not None:
                self.rejected += 1
                self._last_reject_reason = reason
                self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
                # 401 for auth failures, 400 for everything else (never reveals the secret)
                code = 401 if reason in (MISSING_SECRET, BAD_SECRET) else 400
                self._persist_locked()
                logger.info("tradingview alert REJECTED: reason=%s (received=%d valid=%d)",
                            reason, self.received, self.valid)
                return code, {"ok": False, "reason": reason, "observe_only": True}
            if ev.event_id in self._seen_set:
                self.rejected += 1
                self.reject_reasons[DUPLICATE_EVENT_ID] = \
                    self.reject_reasons.get(DUPLICATE_EVENT_ID, 0) + 1
                self._persist_locked()
                return 200, {"ok": True, "duplicate": True, "reason": DUPLICATE_EVENT_ID,
                             "event_id": ev.event_id, "observe_only": True}
            # Retired intrahour TF (55m): acknowledge webhook but do not persist or count.
            if self._is_retired_timeframe(ev.timeframe):
                self.received = max(0, int(self.received) - 1)  # undo the bump above
                logger.info(
                    "tradingview alert IGNORED retired_tf: %s tf=%s id=%s",
                    ev.symbol, ev.timeframe, ev.event_id,
                )
                return 200, {
                    "ok": True,
                    "accepted": True,
                    "ignored": True,
                    "reason": "retired_timeframe",
                    "event_id": ev.event_id,
                    "observe_only": True,
                }
            # accept (observe-only): record dedupe id, counters, latest, pending queue
            self._seen.append(ev.event_id)
            self._seen_set.add(ev.event_id)
            if len(self._seen_set) > self._seen.maxlen:
                # keep the set bounded to the deque window
                self._seen_set = set(self._seen)
            self.valid += 1
            self._last_valid_at = now
            self._last_reject_reason = None
            self.latest = ev
            store_sym = self._storage_symbol(ev.symbol)
            self.latest_by_symbol[store_sym] = ev
            _tf_key = str(ev.timeframe or "?")
            # Active council TFs only — retired/dropped alerts are accepted (observe) but not tracked.
            if (_tf_key not in self.drop_timeframes
                    and _tf_key in self.mtf_timeframes):
                self.latest_by_tf[(store_sym, _tf_key)] = (ev, float(now))
            self.valid_by_symbol[store_sym] = self.valid_by_symbol.get(store_sym, 0) + 1
            self._record_alert_history(ev)
            self._pending.append(ev)
            self._persist_locked()
            for cb in list(self._on_accepted):
                try:
                    cb(ev, now=now)
                except Exception:  # noqa: BLE001
                    logger.debug("tv on_accepted callback error", exc_info=True)
            logger.info("tradingview alert ACCEPTED (observe-only): %s %s tf=%s strength=%s id=%s "
                        "(valid=%d)", ev.symbol, ev.direction, ev.timeframe, ev.strength,
                        ev.event_id, self.valid)
            return 200, {"ok": True, "accepted": True, "event_id": ev.event_id,
                         "direction": ev.direction, "observe_only": True,
                         "note": "candidate-signal only; cannot place/resize/bypass a trade"}

    # -- engine-side consumption -------------------------------------------- #
    def drain_pending(self) -> list:
        with self._lock:
            out, self._pending = self._pending, []
            self.consumed += len(out)
            return out

    def mtf_confirmation(self, *, symbol: Optional[str] = None,
                         now: Optional[float] = None,
                         tfs: Optional[Iterable[str]] = None) -> dict:
        """Cross-timeframe confirmation across configured chart alerts (default 5/10/15m).

        ``confirm`` — fast pair (first two TFs, default 5m+10m): confirmed_up/down, conflict,
          single_tf, none. Used by the MTF conflict gate.
        ``confirm_mtf`` / ``confirm_{N}tf`` — all configured TFs: confirmed_up_mtf,
          partial_up_mtf, conflict_mtf, etc. Grok reads this via ``tradingview_trend``.
        OBSERVE-ONLY — graded feature only."""
        now = now if now is not None else time.time()
        sym = self._mtf_symbol(symbol)
        tfs = tuple(str(t) for t in (tfs if tfs is not None else self.mtf_timeframes))
        windows = self.confirm_windows_by_tf

        def _fresh(entry, window_s: float):
            if not entry:
                return None
            ev, ts = entry
            return ev if (now - float(ts)) <= float(window_s) else None

        dirs: dict[str, Optional[str]] = {}
        entries: dict[str, Optional[tuple]] = {}
        for tf in tfs:
            entry = self.latest_by_tf.get((sym, tf)) if sym else None
            entries[tf] = entry
            ev = _fresh(entry, windows.get(tf, 360.0))
            dirs[tf] = ev.direction if ev else None

        out: dict = {
            "symbol": sym,
            "mtf_timeframes": list(tfs),
            "mtf_count": len(tfs),
            "confirm_windows_by_tf": {tf: windows[tf] for tf in tfs},
            "fast_pair": list(tfs[:2]) if len(tfs) >= 2 else [],
            "trend_by_tf": {tf: d for tf, d in dirs.items() if d},
        }
        for tf in tfs:
            out[tf_dir_key(tf)] = dirs[tf]
            ent = entries[tf]
            out[tf_age_key(tf)] = (round(now - ent[1], 1) if ent else None)

        # Fast-pair confirm (5m+10m by default) — conflict gate reads ``confirm``.
        if len(tfs) >= 2:
            tf_a, tf_b = tfs[0], tfs[1]
            d_a, d_b = dirs[tf_a], dirs[tf_b]
            if d_a and d_b:
                if d_a == d_b and d_a in ("UP", "DOWN"):
                    out["confirm"] = "confirmed_up" if d_a == "UP" else "confirmed_down"
                    out["direction"] = d_a
                else:
                    out["confirm"] = "conflict"
                    out["direction"] = None
            elif d_a or d_b:
                out["confirm"] = "single_tf"
                out["direction"] = d_a or d_b
            else:
                out["confirm"] = "none"
                out["direction"] = None
        elif tfs:
            d0 = dirs[tfs[0]]
            out["confirm"] = "single_tf" if d0 else "none"
            out["direction"] = d0
        else:
            out["confirm"] = "none"
            out["direction"] = None

        # Slow-TF alignment: fast pair (first two TFs) vs the third configured TF.
        slow_tf = tfs[2] if len(tfs) >= 3 else (tfs[-1] if tfs else None)
        d_slow = dirs.get(slow_tf) if slow_tf else None
        fast_dir = out.get("direction")
        if len(tfs) >= 2 and fast_dir in ("UP", "DOWN"):
            if d_slow == fast_dir:
                out["confirm_3tf"] = (
                    "confirmed_up_3tf" if fast_dir == "UP" else "confirmed_down_3tf")
                out["direction_3tf"] = fast_dir
            elif d_slow and d_slow != fast_dir:
                out["confirm_3tf"] = "conflict_%sm" % slow_tf
                out["direction_3tf"] = None
            else:
                out["confirm_3tf"] = "partial_3tf"
                out["direction_3tf"] = fast_dir
        elif d_slow and (len(tfs) >= 2 and (dirs[tfs[0]] or dirs[tfs[1]])):
            out["confirm_3tf"] = "partial_3tf"
            out["direction_3tf"] = d_slow if not fast_dir else None
        elif d_slow:
            out["confirm_3tf"] = "single_tf_%sm" % slow_tf
            out["direction_3tf"] = d_slow
        else:
            out["confirm_3tf"] = out["confirm"]
            out["direction_3tf"] = out.get("direction")

        dirs_all = [d for d in dirs.values() if d in ("UP", "DOWN")]
        out["trend_fresh_count"] = len(dirs_all)
        n = len(tfs)
        confirm_ntf = "confirm_%dtf" % n
        direction_ntf = "direction_%dtf" % n

        def _align_all(dirs_list: list[str], *, suffix: str) -> tuple[str, Optional[str]]:
            if len(dirs_list) == n and n > 0:
                if len(set(dirs_list)) == 1:
                    tag = "confirmed_up_%s" if dirs_list[0] == "UP" else "confirmed_down_%s"
                    return tag % suffix, dirs_list[0]
                return "conflict_%s" % suffix, None
            if len(dirs_list) >= 2:
                ups = sum(1 for d in dirs_list if d == "UP")
                downs = len(dirs_list) - ups
                if ups > downs:
                    return "partial_up_%s" % suffix, "UP"
                if downs > ups:
                    return "partial_down_%s" % suffix, "DOWN"
                return "conflict_%s" % suffix, None
            if len(dirs_list) == 1:
                return "single_tf", dirs_list[0]
            return "none", None

        confirm_mtf, direction_mtf = _align_all(dirs_all, suffix="mtf")
        out["confirm_mtf"] = confirm_mtf
        out["direction_mtf"] = direction_mtf
        confirm_n, direction_n = _align_all(dirs_all, suffix="%dtf" % n)
        out[confirm_ntf] = confirm_n
        out[direction_ntf] = direction_n
        return out

    def latest_feature_for_symbol(self, symbol: str, *, now: Optional[float] = None) -> Optional[dict]:
        """Latest valid alert feature for one storage symbol (multi-asset 1h directional)."""
        store = self._storage_symbol(symbol)
        with self._lock:
            ev = self.latest_by_symbol.get(store)
        if ev is None:
            return None
        return ev.as_feature(now=now)

    def latest_feature(self, *, now: Optional[float] = None, symbol: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            if symbol is not None:
                ev = self.latest_by_symbol.get(self._storage_symbol(symbol))
            else:
                ev = self.latest
        if ev is None:
            return None
        feat = ev.as_feature(now=now)
        # attach cross-timeframe confirmation so the engine can SEE and GRADE every chart TF,
        # not just whichever alert arrived last. Observe-only.
        mtf = self.mtf_confirmation(symbol=(symbol or ev.symbol), now=now)
        feat["tf_confirm"] = mtf.get("confirm")
        feat["tf_confirm_direction"] = mtf.get("direction")
        feat["tf_confirm_3tf"] = mtf.get("confirm_3tf")
        feat["tf_confirm_3tf_direction"] = mtf.get("direction_3tf")
        feat["tf_confirm_mtf"] = mtf.get("confirm_mtf")
        feat["tf_confirm_mtf_direction"] = mtf.get("direction_mtf")
        feat["mtf_timeframes"] = mtf.get("mtf_timeframes")
        feat["mtf_count"] = mtf.get("mtf_count")
        feat["trend_by_tf"] = mtf.get("trend_by_tf")
        feat["trend_fresh_count"] = mtf.get("trend_fresh_count")
        n = int(mtf.get("mtf_count") or 0)
        if n:
            feat["tf_confirm_%dtf" % n] = mtf.get("confirm_%dtf" % n)
            feat["tf_confirm_%dtf_direction" % n] = mtf.get("direction_%dtf" % n)
        for tf in mtf.get("mtf_timeframes") or []:
            feat[tf_dir_key(tf)] = mtf.get(tf_dir_key(tf))
            feat[tf_age_key(tf)] = mtf.get(tf_age_key(tf))
        return feat

    def report(self) -> dict:
        with self._lock:
            now = time.time()
            last_valid = float(self._last_valid_at or (self.latest.received_at if self.latest else 0) or 0)
            last_recv = float(self._last_received_at or 0)
            since_valid = round(now - last_valid, 1) if last_valid > 0 else None
            since_recv = round(now - last_recv, 1) if last_recv > 0 else None
            return {
                "enabled": True,
                "tradingview_observe_only": True,
                "tradingview_alerts_received": self.received,
                "tradingview_alerts_valid": self.valid,
                "tradingview_alerts_rejected": self.rejected,
                "tradingview_alerts_consumed_as_features": self.consumed,
                "tradingview_reject_reasons": dict(self.reject_reasons),
                "tradingview_latest_signal": (self.latest.to_dict() if self.latest else None),
                "tradingview_latest_by_symbol": {s: e.to_dict()
                                                 for s, e in self.latest_by_symbol.items()},
                "tradingview_valid_by_symbol": dict(self.valid_by_symbol),
                "tradingview_latest_by_timeframe": {
                    "%s@%s" % (s, tf): {
                        "direction": e.direction,
                        "strength": e.strength,
                        "signal_level": e.signal_level,
                        "ts": _ts,                       # received time -> dashboard can flag stale
                        "indicator_name": e.indicator_name,
                    }
                    for (s, tf), (e, _ts) in self.latest_by_tf.items()},
                "tradingview_mtf_confirmation": self.mtf_confirmation(symbol=self.feature_symbol),
                "tradingview_alert_history": self.alert_history_snapshot(),
                "tradingview_mtf_timeframes": list(self.mtf_timeframes),
                "tradingview_last_received_at": last_recv or None,
                "tradingview_last_valid_at": last_valid or None,
                "tradingview_seconds_since_valid": since_valid,
                "tradingview_seconds_since_received": since_recv,
                "tradingview_last_reject_reason": self._last_reject_reason,
                "tradingview_feature_symbol": self.feature_symbol,
                "allowed_symbols": sorted(self.allowed_symbols),
                "bot_name": self.bot_name,
                "dedupe_tracked": len(self._seen_set),
                "note": ("TradingView alerts are candidate signals only — they cannot place, "
                         "resize, or bypass trades; the strategy + execution gate remain "
                         "the sole trade authority."),
            }

    def scope_since(self, epoch_ts: float) -> int:
        """Drop intake snapshots before *epoch_ts* and reset lifetime counters to post-epoch data.

        Pre-reset ``received``/``valid``/``rejected`` totals are not recoverable for rejects;
        counters are rebuilt from retained valid events only. Returns count of retained event_ids."""
        since = float(epoch_ts)
        with self._lock:
            new_history: dict[str, deque] = {}
            hist_event_ids: set[str] = set()
            for sym, dq in list(self.alert_history_by_symbol.items()):
                kept_rows = [r for r in dq if float(r.get("received_at") or 0) >= since]
                if not kept_rows:
                    continue
                ndq = deque(maxlen=self.alert_history_per_symbol)
                for row in kept_rows:
                    ndq.append(row)
                    eid = str(row.get("event_id") or "").strip()
                    if eid:
                        hist_event_ids.add(eid)
                new_history[sym] = ndq
            self.alert_history_by_symbol = new_history

            self.latest_by_symbol = {
                s: e for s, e in self.latest_by_symbol.items()
                if float(e.received_at or 0) >= since
            }
            self.latest_by_tf = {
                k: v for k, v in self.latest_by_tf.items()
                if float(v[1]) >= since and float(v[0].received_at or 0) >= since
            }
            if self.latest is not None and float(self.latest.received_at or 0) < since:
                self.latest = None
            if self.latest_by_symbol:
                self.latest = max(self.latest_by_symbol.values(),
                                  key=lambda e: float(e.received_at or 0))

            event_ids = set(hist_event_ids)
            for ev in self.latest_by_symbol.values():
                event_ids.add(ev.event_id)
            for ev, _ts in self.latest_by_tf.values():
                event_ids.add(ev.event_id)

            self._seen.clear()
            self._seen_set = set()
            for eid in event_ids:
                self._seen.append(eid)
                self._seen_set.add(eid)

            self.valid_by_symbol = {sym: len(dq) for sym, dq in self.alert_history_by_symbol.items()}
            for sym in self.latest_by_symbol:
                if sym not in self.valid_by_symbol:
                    self.valid_by_symbol[sym] = 1

            self.valid = sum(self.valid_by_symbol.values())
            self.received = self.valid
            self.rejected = 0
            self.consumed = 0
            self.reject_reasons = {}
            self._pending = []
            self._last_reject_reason = None
            if self.latest is not None:
                self._last_valid_at = float(self.latest.received_at or 0)
            else:
                self._last_valid_at = 0.0

            self._persist_locked()
            return len(event_ids)

    # -- persistence (dedupe survives restarts) ----------------------------- #
    def _persist_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({
                "received": self.received, "valid": self.valid, "rejected": self.rejected,
                "consumed": self.consumed, "reject_reasons": dict(self.reject_reasons),
                "seen_ids": list(self._seen),
                "latest": (self.latest.to_dict() if self.latest else None),
                "latest_by_symbol": {s: e.to_dict() for s, e in self.latest_by_symbol.items()},
                "latest_by_tf": [{"symbol": s, "tf": tf, "ts": ts, "ev": e.to_dict()}
                                 for (s, tf), (e, ts) in self.latest_by_tf.items()],
                "valid_by_symbol": dict(self.valid_by_symbol),
                "alert_history_per_symbol": self.alert_history_per_symbol,
                "alert_history_by_symbol": {
                    sym: list(dq) for sym, dq in self.alert_history_by_symbol.items()
                },
                "rsi_div_history_per_symbol": self.rsi_div_history_per_symbol,
                "rsi_div_history_by_symbol": {
                    sym: list(dq) for sym, dq in self.rsi_div_history_by_symbol.items()
                },
                "rsi_band_history_per_symbol": self.rsi_band_history_per_symbol,
                "rsi_band_history_by_symbol": {
                    sym: list(dq) for sym, dq in self.rsi_band_history_by_symbol.items()
                },
            }, default=str, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001 — persistence never breaks intake
            pass

    def _load_state(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        self.received = int(data.get("received", 0) or 0)
        self.valid = int(data.get("valid", 0) or 0)
        self.rejected = int(data.get("rejected", 0) or 0)
        self.consumed = int(data.get("consumed", 0) or 0)
        self.reject_reasons = {k: int(v or 0) for k, v in (data.get("reject_reasons") or {}).items()}
        for sid in (data.get("seen_ids") or []):
            self._seen.append(sid)
        self._seen_set = set(self._seen)
        # restore the last signal(s) so the report keeps showing them across restarts
        self.latest = _event_from_dict(data.get("latest"))
        if self.latest is not None and float(self.latest.received_at or 0) > 0:
            self._last_valid_at = float(self.latest.received_at)
        self.latest_by_symbol = {}
        for sym, ed in (data.get("latest_by_symbol") or {}).items():
            ev = _event_from_dict(ed)
            if ev is not None:
                self.latest_by_symbol[sym] = ev
        self.valid_by_symbol = {k: int(v or 0)
                                for k, v in (data.get("valid_by_symbol") or {}).items()}
        self.latest_by_tf = {}
        for row in (data.get("latest_by_tf") or []):
            ev = _event_from_dict(row.get("ev"))
            if ev is not None:
                self.latest_by_tf[(row.get("symbol"), str(row.get("tf")))] = (ev, float(row.get("ts") or 0.0))
        # Hard FIFO cap from runtime config (operator: last 50 alerts/symbol).
        # Do NOT inflate from a larger persisted value — that would keep >50 forever.
        self.alert_history_per_symbol = max(1, int(self.alert_history_per_symbol or 1))
        self.rsi_div_history_per_symbol = max(1, int(self.rsi_div_history_per_symbol or 1))
        self.rsi_band_history_per_symbol = max(1, int(self.rsi_band_history_per_symbol or 1))
        self.alert_history_by_symbol = {}
        self.rsi_div_history_by_symbol = {}
        self.rsi_band_history_by_symbol = {}
        # Load RSI band FIFO (dedicated key).
        for sym, rows in (data.get("rsi_band_history_by_symbol") or {}).items():
            dq = deque(maxlen=self.rsi_band_history_per_symbol)
            for row in (rows or []):
                if isinstance(row, dict) and row.get("event_id"):
                    dq.append(row)
            if dq:
                self.rsi_band_history_by_symbol[str(sym)] = dq
        # Load RSI overlay FIFO (dedicated key).
        for sym, rows in (data.get("rsi_div_history_by_symbol") or {}).items():
            dq = deque(maxlen=self.rsi_div_history_per_symbol)
            for row in (rows or []):
                if isinstance(row, dict) and row.get("event_id"):
                    dq.append(row)
            if dq:
                self.rsi_div_history_by_symbol[str(sym)] = dq
        # Path FIFO: strip RSI-div rows into overlay FIFO (migration from mixed history).
        for sym, rows in (data.get("alert_history_by_symbol") or {}).items():
            path_dq = deque(maxlen=self.alert_history_per_symbol)
            rsi_dq = self.rsi_div_history_by_symbol.get(str(sym))
            if rsi_dq is None:
                rsi_dq = deque(maxlen=self.rsi_div_history_per_symbol)
            band_dq = self.rsi_band_history_by_symbol.get(str(sym))
            if band_dq is None:
                band_dq = deque(maxlen=self.rsi_band_history_per_symbol)
            for row in (rows or []):
                if not isinstance(row, dict) or not row.get("event_id"):
                    continue
                kind = str(row.get("signal_kind") or "").strip().lower()
                level = str(row.get("signal_level") or "").strip().upper()
                if kind == "rsi_band":
                    band_dq.append(row)
                elif kind == "rsi_divergence" or "DIV" in level:
                    rsi_dq.append(row)
                else:
                    path_dq.append(row)
            if path_dq:
                self.alert_history_by_symbol[str(sym)] = path_dq
            if rsi_dq:
                self.rsi_div_history_by_symbol[str(sym)] = rsi_dq
            if band_dq:
                self.rsi_band_history_by_symbol[str(sym)] = band_dq
        had_legacy_tf = any(
            str(k[1]) in (LEGACY_MTF_TFS | self.drop_timeframes) for k in self.latest_by_tf)
        had_unsup = UNSUPPORTED_SYMBOL in self.reject_reasons
        self._canonicalize_storage()
        scrubbed = self._scrub_legacy_reject_stats()
        purged = self._purge_retired_timeframe_data()
        if had_legacy_tf or had_unsup or scrubbed or purged:
            self._persist_locked()
