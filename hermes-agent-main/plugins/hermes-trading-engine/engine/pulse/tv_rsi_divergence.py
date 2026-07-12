"""RSI divergence analysis — teach Grok/bot how divergence works + live history.

Grounded in TradingView's official RSI documentation (Wilder 1978) and the
operator's ``RSI Divergence Indicator`` Pine script (pivot-based detection).
Regular bull/bear webhooks drive confirm/fade; hidden types are taught but not
webhooke'd. Separate FIFO from bar-close path and rsi_band heartbeats.
"""

from __future__ import annotations

from typing import Optional

from engine.pulse.tv_15m_price_path import path_symbol_candidates
from engine.pulse.tv_rsi_overlay import (
    filter_rsi_divergence,
    latest_rsi_overlay,
    rsi_overlay_decision,
)

# Official TradingView RSI reference (Wilder + built-in divergence notes).
TRADINGVIEW_OFFICIAL_RSI = {
    "source": "TradingView Help Center — Relative Strength Index (RSI)",
    "source_url": "https://www.tradingview.com/support/solutions/43000502338-relative-strength-index-rsi/",
    "author": "J. Welles Wilder Jr. (New Concepts in Technical Trading Systems, 1978)",
    "rsi_scale": "0–100 momentum oscillator; closer to 0 = weak momentum, closer to 100 = strong",
    "default_length": 14,
    "wilder_overbought": 70,
    "wilder_oversold": 30,
    "wilder_neutral_band": "30–70 neutral; ~50 = no trend",
    "formula_summary": "RSI = 100 - 100/(1 + RS); RS = avg gain / avg loss over n bars",
    "built_in_divergence_setting": (
        "TradingView's built-in RSI has 'Calculate Divergence' checkbox in Inputs — "
        "highlights where RSI direction diverges from price (bullish/bearish flag)."
    ),
    "wilder_divergence_definition": (
        "RSI Divergence = price action disagrees with RSI. Interpret as impending reversal."
    ),
    "wilder_bullish_divergence": (
        "Price makes a new low but RSI makes a higher low → buying opportunity (Wilder)."
    ),
    "wilder_bearish_divergence": (
        "Price makes a new high but RSI makes a lower high → selling opportunity (Wilder)."
    ),
    "cardwell_trend_context": (
        "Andrew Cardwell: bullish divergence usually only in bearish trends; bearish "
        "divergence only in bullish trends. Divergence often causes a brief correction, "
        "not a full trend reversal — use to confirm trends, not only anticipate reversals."
    ),
    "cardwell_positive_reversal": (
        "Bullish trend only: price makes higher low while RSI makes lower low — price "
        "proceeds to rise (momentum lags price)."
    ),
    "cardwell_negative_reversal": (
        "Bearish trend only: price makes lower high while RSI makes higher high — price "
        "proceeds to fall (momentum lags price)."
    ),
    "divergence_limitations": (
        "TradingView: divergence is lagging, not always at reversals, combine with other "
        "tools — never rely on divergence alone."
    ),
    "failure_swings": {
        "bullish": [
            "RSI drops below 30 (oversold)",
            "RSI bounces above 30",
            "RSI pulls back but stays above 30",
            "RSI breaks above its previous high",
        ],
        "bearish": [
            "RSI rises above 70 (overbought)",
            "RSI drops below 70",
            "RSI rises slightly but stays below 70",
            "RSI drops below its previous low",
        ],
        "note": "Failure swings are RSI-only (independent of price); distinct from pivot divergence.",
    },
}

# Operator indicator = standard open-source RSI Divergence Indicator Pine (docs/RSI Divergence Indicator.txt).
OPERATOR_INDICATOR = {
    "name": "Hermes RSI Divergence Indicator",
    "pine_base": "RSI Divergence Indicator (open-source pivot script)",
    "chart_symbols": (
        "1h lane: BINANCE:BTCUSDT + BINANCE:ETHUSDT · 5m; "
        "15m lane: INDEX:BTCUSD + INDEX:ETHUSD · 5m (Chainlink settlement oracle)"
    ),
    "config": {
        "rsi_period": 14,
        "rsi_source": "close",
        "pivot_lookback_left": 5,
        "pivot_lookback_right": 5,
        "pivot_range_min_bars": 5,
        "pivot_range_max_bars": 60,
        "overbought_line": 70,
        "oversold_line": 30,
        "middle_line": 50,
        "webhook_regular_only": True,
        "webhook_hidden": "plot only — not sent to bot",
    },
    "detection_logic": (
        "Finds RSI pivot lows/highs (ta.pivotlow/pivothigh on RSI with lbL=5, lbR=5). "
        "Compares consecutive pivots within 5–60 bars: price swing vs RSI swing must disagree."
    ),
}

# Compact primer Grok reads every bundle — how to reason about divergence.
RSI_DIVERGENCE_PRIMER = {
    "tradingview_official": TRADINGVIEW_OFFICIAL_RSI,
    "operator_indicator": OPERATOR_INDICATOR,
    "definition": (
        "RSI divergence: price pivots and RSI pivots disagree on momentum. TradingView/Wilder: "
        "bullish = price lower low + RSI higher low; bearish = price higher high + RSI lower high. "
        "Our Pine script uses pivot lookback 5/5 and 5–60 bar range between pivots."
    ),
    "regular_bullish": {
        "pattern": "price lower low + RSI higher low",
        "pine_checks": "priceLL = low < prior pivot low; oscHL = RSI > prior pivot RSI low",
        "meaning": (
            "Selling pressure weakening despite new price low — RSI does not confirm the low. "
            "Wilder: buying opportunity; strongest near oversold (<30) or after bearish stretch."
        ),
        "typical_zone": "Often fires from oversold/neutral RSI; stronger when RSI was <40",
        "lean": "up",
        "webhook_to_bot": True,
    },
    "regular_bearish": {
        "pattern": "price higher high + RSI lower high",
        "pine_checks": "priceHH = high > prior pivot high; oscLH = RSI < prior pivot RSI high",
        "meaning": (
            "Buying pressure weakening despite new price high — RSI does not confirm the high. "
            "Wilder: selling opportunity; strongest near overbought (>70) or after bullish stretch."
        ),
        "typical_zone": "Often fires from overbought/neutral RSI; stronger when RSI was >60",
        "lean": "down",
        "webhook_to_bot": True,
    },
    "hidden_bullish": {
        "pattern": "price higher low + RSI lower low",
        "pine_checks": "priceHL = low > prior pivot low; oscLL = RSI < prior pivot RSI low",
        "meaning": "Trend-continuation UP — pullback held, momentum cooled but trend may resume",
        "lean": "up",
        "webhook_to_bot": False,
    },
    "hidden_bearish": {
        "pattern": "price lower high + RSI higher high",
        "pine_checks": "priceLH = high < prior pivot high; oscHH = RSI > prior pivot RSI high",
        "meaning": "Trend-continuation DOWN — rally faded, RSI firm but price failed higher",
        "lean": "down",
        "webhook_to_bot": False,
    },
    "vs_rsi_band": (
        "rsi_band = continuous 30/70 Wilder zones every bar (mean-revert backdrop). "
        "rsi_divergence = sparse pivot event when price vs RSI pivots disagree. "
        "Use band for zone context; divergence for exhaustion timing confirm/fade."
    ),
    "bot_usage": (
        "Observe-only confirm/fade overlay on regular bull/bear only: aligned divergence → "
        "size up; opposed → size down. Never overrides price_path trend. Cardwell: treat "
        "divergence as correction/trend-confirm context on 5m BTC/ETH, not guaranteed reversal."
    ),
    "how_to_read_alerts": (
        "REGULAR_BULL_DIV / regular_bullish → UP lean. REGULAR_BEAR_DIV / regular_bearish → "
        "DOWN lean. Check rsi field + rsi_zone_at_signal. Sparse — no alert between pivots is normal."
    ),
}


def _age_s(row: dict, now: float) -> Optional[float]:
    for key in ("received_at", "bar_time"):
        try:
            t = float(row.get(key))
        except (TypeError, ValueError):
            continue
        if t > 1e12:
            t /= 1000.0
        if t > 0:
            return max(0.0, float(now) - t)
    return None


def classify_divergence(row: dict) -> dict:
    """Normalize divergence kind + trading lean from one alert row."""
    kind = str(row.get("divergence_kind") or "").strip().lower()
    level = str(row.get("signal_level") or "").strip().upper()
    direction = str(row.get("direction") or "").upper()

    if kind in ("regular_bullish",) or level == "REGULAR_BULL_DIV":
        div_type = "regular_bullish"
        lean = "up"
        pattern = RSI_DIVERGENCE_PRIMER["regular_bullish"]["pattern"]
        meaning = RSI_DIVERGENCE_PRIMER["regular_bullish"]["meaning"]
    elif kind in ("regular_bearish",) or level == "REGULAR_BEAR_DIV":
        div_type = "regular_bearish"
        lean = "down"
        pattern = RSI_DIVERGENCE_PRIMER["regular_bearish"]["pattern"]
        meaning = RSI_DIVERGENCE_PRIMER["regular_bearish"]["meaning"]
    elif "hidden" in kind or "HIDDEN" in level:
        if direction == "UP" or "bull" in kind:
            div_type = "hidden_bullish"
            pattern = RSI_DIVERGENCE_PRIMER["hidden_bullish"]["pattern"]
            meaning = RSI_DIVERGENCE_PRIMER["hidden_bullish"]["meaning"]
        else:
            div_type = "hidden_bearish"
            pattern = RSI_DIVERGENCE_PRIMER["hidden_bearish"]["pattern"]
            meaning = RSI_DIVERGENCE_PRIMER["hidden_bearish"]["meaning"]
        lean = "up" if "bull" in div_type else "down"
    else:
        div_type = kind or level.lower() or "unknown"
        lean = "up" if direction == "UP" else ("down" if direction == "DOWN" else None)
        pattern = None
        meaning = None

    try:
        rsi = float(row.get("rsi")) if row.get("rsi") is not None else None
    except (TypeError, ValueError):
        rsi = None

    zone_hint = None
    if rsi is not None:
        if rsi <= 30:
            zone_hint = "oversold"
        elif rsi >= 70:
            zone_hint = "overbought"
        else:
            zone_hint = "neutral"

    return {
        "divergence_type": div_type,
        "lean": lean,
        "direction": direction,
        "pattern": pattern,
        "meaning": meaning,
        "rsi": rsi,
        "rsi_zone_at_signal": zone_hint,
        "signal_level": row.get("signal_level"),
        "divergence_kind": row.get("divergence_kind"),
        "strength": row.get("strength"),
        "price": row.get("price"),
    }


def summarize_divergence_history(rows: list) -> dict:
    """Stats over oldest→newest regular divergence alerts."""
    div_rows = filter_rsi_divergence(rows)
    if not div_rows:
        return {"n": 0, "has_fresh_signal": False}

    bulls = bears = 0
    interpreted = []
    for r in div_rows:
        info = classify_divergence(r)
        interpreted.append(info)
        if info["divergence_type"] == "regular_bullish":
            bulls += 1
        elif info["divergence_type"] == "regular_bearish":
            bears += 1

    last = interpreted[-1]
    sequence = [i["divergence_type"] for i in interpreted[-6:]]
    return {
        "n": len(div_rows),
        "bull_count": bulls,
        "bear_count": bears,
        "last_type": last.get("divergence_type"),
        "last_lean": last.get("lean"),
        "last_rsi": last.get("rsi"),
        "last_rsi_zone": last.get("rsi_zone_at_signal"),
        "recent_sequence": sequence,
        "has_fresh_signal": True,
    }


def rsi_divergence_snapshot(
    rows: list,
    *,
    now: float,
    max_age_s: float = 2700.0,
    history_n: int = 10,
    trade_side: Optional[str] = None,
) -> Optional[dict]:
    """Full divergence context for Grok — primer + latest + history + confirm/fade."""
    div_rows = filter_rsi_divergence(rows)
    if not div_rows:
        return {
            "enabled": True,
            "has_signal": False,
            "primer": RSI_DIVERGENCE_PRIMER,
            "history_summary": {"n": 0, "has_fresh_signal": False},
            "note": "No RSI divergence alerts in FIFO yet — indicator fires on pivot disagreement only.",
            "observe_only": True,
            "source": "rsi_divergence_5m",
        }

    latest_raw = latest_rsi_overlay(div_rows, now=float(now), max_age_s=float(max_age_s))
    hist = div_rows[-max(1, int(history_n or 10)):]
    summary = summarize_divergence_history(hist)

    latest_interp = None
    overlay = None
    confirm_fade_by_side = None
    if latest_raw:
        # Find matching raw row for full interpretation
        best_row = div_rows[-1]
        for r in reversed(div_rows):
            if str(r.get("signal_level")) == str(latest_raw.get("signal_level")):
                best_row = r
                break
        latest_interp = classify_divergence(best_row)
        latest_interp["age_s"] = latest_raw.get("age_s")
        latest_interp["indicator_name"] = latest_raw.get("indicator_name")
        latest_interp["symbol"] = latest_raw.get("symbol")
        overlay = latest_raw
        confirm_fade_by_side = {
            "up": rsi_overlay_decision(side="up", overlay=overlay),
            "down": rsi_overlay_decision(side="down", overlay=overlay),
        }
        if trade_side:
            confirm_fade_by_side["proposed"] = rsi_overlay_decision(
                side=trade_side, overlay=overlay)

    recent = []
    for r in hist[-8:]:
        item = classify_divergence(r)
        item["bar_time"] = r.get("bar_time")
        item["received_at"] = r.get("received_at")
        recent.append(item)

    return {
        "enabled": True,
        "has_signal": latest_interp is not None,
        "primer": RSI_DIVERGENCE_PRIMER,
        "latest": latest_interp,
        "overlay": overlay,
        "confirm_fade_by_side": confirm_fade_by_side,
        "history_summary": summary,
        "recent_signals": recent,
        "observe_only": True,
        "source": "rsi_divergence_5m",
        "note": (
            "Read primer.tradingview_official (Wilder/Cardwell) + operator_indicator (Pine pivot "
            "logic) first. latest = freshest regular divergence webhook; confirm_fade_by_side "
            "shows bot sizing. Sparse vs rsi_band — no pivot = no alert."
        ),
    }


def resolve_rsi_divergence_from_intake(
    intake,
    symbol: Optional[str],
    *,
    now: float,
    max_age_s: float = 2700.0,
    history_n: int = 10,
    trade_side: Optional[str] = None,
) -> Optional[dict]:
    """RSI divergence analysis from the lane-routed symbol FIFO only."""
    if intake is None:
        return None
    for cand in path_symbol_candidates(symbol, strict_lane=True):
        try:
            rows = list(intake.rsi_div_history_for_symbol(cand) or [])
        except Exception:  # noqa: BLE001
            rows = []
        snap = rsi_divergence_snapshot(
            rows,
            now=float(now),
            max_age_s=float(max_age_s),
            history_n=history_n,
            trade_side=trade_side,
        )
        if snap:
            return {**snap, "resolved_symbol": cand}
    return None
