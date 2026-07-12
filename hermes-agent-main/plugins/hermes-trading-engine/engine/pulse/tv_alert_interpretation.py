"""Unified TradingView alert interpretation — train Grok/bot on official TV semantics.

Grounded in TradingView Help Center (Wilder RSI, divergence indicator, Cardwell
trend confirmations). Synthesizes lane-routed FIFOs into one structured read per window.
"""

from __future__ import annotations

from typing import Optional

from engine.pulse.tradingview import (
    TV_CHART_LANE_1H,
    tv_chart_symbol_for_asset,
    tv_lane_kind,
)

# Consolidated reading guide — Grok reads this every bundle via tradingview_alert_interpretation.
TV_ALERT_GUIDE = {
    "sources": [
        {
            "name": "TradingView RSI (Wilder 1978)",
            "url": "https://www.tradingview.com/support/solutions/43000502338-relative-strength-index-rsi/",
        },
        {
            "name": "TradingView RSI Divergence Indicator",
            "url": "https://www.tradingview.com/support/solutions/43000589127-rsi-divergence-indicator/",
        },
    ],
    "signal_kinds": {
        "bar_close_5m": (
            "Hermes BarClose heartbeat every 5m bar — PRIMARY price pattern. "
            "Plot short_path OHLC oldest→newest; short lean drives entry side."
        ),
        "rsi_band": (
            "Continuous RSI 30/70 zone every bar (Wilder OB/OS). "
            "Oversold (<30) = mean-revert UP lean; overbought (>70) = DOWN lean. "
            "band_event crosses (enter/exit 30/70) mark timing."
        ),
        "rsi_divergence": (
            "Sparse pivot event: price pivot disagrees with RSI pivot. "
            "Regular bull = price LL + RSI HL (UP lean); regular bear = price HH + RSI LH "
            "(DOWN lean). Confirm/fade overlay only — never overrides price path."
        ),
    },
    "lane_routing": {
        "1h": "BINANCE *USDT charts (BTCUSDT/ETHUSDT) — leading liquidity for hourly windows.",
        "15m": "Chainlink INDEX *USD charts (BTCUSD/ETHUSD) — settlement oracle for 15m windows.",
        "rule": "Never cross-feed: 1h reads *USDT FIFO only; 15m reads INDEX *USD FIFO only.",
    },
    "wilder_divergence": {
        "bullish": "Price lower low + RSI higher low → buying opportunity (reversal signal).",
        "bearish": "Price higher high + RSI lower high → selling opportunity (reversal signal).",
    },
    "cardwell_trend_context": {
        "divergence_in_trend": (
            "Bullish divergence usually only in bearish trends; bearish only in bullish trends. "
            "Divergence often causes a brief correction, NOT a full trend reversal — use to "
            "confirm trends as much as anticipate reversals."
        ),
        "positive_reversal": (
            "Bullish trend only: price higher low + RSI lower low → price rises (momentum lags)."
        ),
        "negative_reversal": (
            "Bearish trend only: price lower high + RSI higher high → price falls (momentum lags)."
        ),
    },
    "failure_swings": {
        "note": "RSI-only 4-step pattern (independent of price pivots) — distinct from divergence.",
        "bullish": "RSI <30 → bounce >30 → pullback stays >30 → breaks prior RSI high.",
        "bearish": "RSI >70 → drop <70 → bounce stays <70 → breaks prior RSI low.",
    },
    "limitations": (
        "TradingView: divergence is lagging, not always present at reversals, and should be "
        "combined with other tools — never trade divergence alone."
    ),
    "decision_hierarchy": [
        "1_price_path_short_lean (bar_close_5m short_path)",
        "2_rsi_band_zone_and_crosses (30/70 backdrop)",
        "3_rsi_divergence_confirm_fade (sparse pivot overlay)",
        "4_regime_path_context (HTF alignment)",
        "5_per_tf_ladder_and_2h_review (when present)",
    ],
}


def _lane_chart_symbols(lane: str) -> str:
    btc = tv_chart_symbol_for_asset("btc", lane) or "BTCUSD"
    eth = tv_chart_symbol_for_asset("eth", lane) or "ETHUSD"
    return "%s + %s" % (btc, eth)


def tv_grok_reading_guide(*, window_seconds: int, series_label: str = "") -> str:
    """Lane-specific TV reading instructions for grok_task.tv_role."""
    ws = int(window_seconds or 300)
    lane = tv_lane_kind(window_seconds=ws, series_label=series_label)
    charts = _lane_chart_symbols(lane)
    feed = "Binance USDT" if lane == TV_CHART_LANE_1H else "Chainlink INDEX USD"

    if ws >= 3600:
        return (
            "Lane 1h → %s (%s). PRIMARY: tradingview_15m_price_path.price_pattern "
            "(5m BarClose short_path last ~8 bars ≈ 40m). Plot short_path OHLC oldest→newest. "
            "RSI BAND (tradingview_rsi_band): Wilder 30/70 — oversold→UP, overbought→DOWN. "
            "RSI DIVERGENCE (tradingview_rsi_divergence): read primer + confirm_fade_by_side; "
            "regular bull/bear only. Cardwell: divergence often = brief correction. "
            "ALERT INTERPRETATION: tradingview_alert_interpretation — synthesized lean + "
            "signal_agreement + grok_instructions. Also 2h_review + per-TF ladder. "
            "Settlement = Chainlink open/close."
            % (charts, feed)
        )
    if ws >= 900:
        return (
            "Lane 15m → %s (%s, settlement oracle). PRIMARY: BarClose 5m short_path "
            "(last 6–8 bars ≈ 30–40m) — highest weight for side. regime_path_tail = HTF only. "
            "When short/regime ALIGN → high conviction; DIVERGENT → prefer short, size down. "
            "RSI BAND: 30/70 zone + band_event crosses for timing. "
            "RSI DIVERGENCE: primer.tradingview_official (Wilder/Cardwell) + operator_indicator; "
            "regular bull = price LL + RSI HL; regular bear = price HH + RSI LH. "
            "Read tradingview_alert_interpretation.composite_lean + confirm_fade. "
            "Settlement = Chainlink — TV observe-only context."
            % (charts, feed)
        )
    return (
        "Lane 5m → %s (%s). PRIMARY: price_pattern short_path from BarClose 5m. "
        "RSI band 30/70 + divergence primer; overlay confirm/fade only. "
        "Read tradingview_alert_interpretation for synthesized lean."
        % (charts, feed)
    )


def _extract_path_lean(price_path: Optional[dict]) -> dict:
    pp = (price_path or {}).get("price_pattern") or {}
    focus = (price_path or {}).get("focus") or {}
    lean = pp.get("trade_lean") or focus.get("trade_lean") or focus.get("lean")
    return {
        "lean": lean,
        "alignment": pp.get("alignment") or focus.get("alignment"),
        "confidence": pp.get("confidence") or focus.get("confidence"),
        "short_pattern": (pp.get("short_pattern") or
                          ((focus.get("short_term") or {}).get("trend") or {}).get("pattern")),
        "regime_pattern": (pp.get("regime_pattern") or
                           ((focus.get("regime") or {}).get("trend") or {}).get("pattern")),
        "short_streak_dir": (pp.get("short_streak_dir") or
                             ((focus.get("short_term") or {}).get("trend") or {})
                             .get("current_streak_dir")),
    }


def _extract_band_lean(rsi_band: Optional[dict]) -> dict:
    b = rsi_band or {}
    return {
        "lean": b.get("lean"),
        "rsi": b.get("rsi"),
        "rsi_zone": b.get("rsi_zone"),
        "band_event": b.get("band_event"),
        "age_s": b.get("age_s"),
        "recent_crosses": ((b.get("history_summary") or {}).get("recent_crosses") or [])[-3:],
    }


def _extract_div_lean(rsi_div: Optional[dict]) -> dict:
    d = rsi_div or {}
    latest = d.get("latest") or {}
    return {
        "has_signal": bool(d.get("has_signal")),
        "lean": latest.get("lean"),
        "divergence_type": latest.get("divergence_type"),
        "rsi_zone_at_signal": latest.get("rsi_zone_at_signal"),
        "age_s": latest.get("age_s"),
        "confirm_fade_by_side": d.get("confirm_fade_by_side"),
        "history_summary": d.get("history_summary"),
    }


def _score_signal_agreement(
    *,
    path_lean: Optional[str],
    band_lean: Optional[str],
    div_lean: Optional[str],
) -> dict:
    """Count how many active TV signals agree on up/down."""
    votes = {"up": 0, "down": 0}
    sources = []
    for name, lean in (("price_path", path_lean), ("rsi_band", band_lean),
                       ("rsi_divergence", div_lean)):
        l = str(lean or "").lower()
        if l in ("up", "down"):
            votes[l] += 1
            sources.append({"source": name, "lean": l})

    up_v = votes["up"]
    down_v = votes["down"]
    if up_v >= 2 and down_v == 0:
        agreement = "bullish_consensus"
        composite = "up"
        confidence = "high" if up_v == 3 else "medium"
    elif down_v >= 2 and up_v == 0:
        agreement = "bearish_consensus"
        composite = "down"
        confidence = "high" if down_v == 3 else "medium"
    elif up_v == 1 and down_v == 0:
        agreement = "single_bullish"
        composite = "up"
        confidence = "low"
    elif down_v == 1 and up_v == 0:
        agreement = "single_bearish"
        composite = "down"
        confidence = "low"
    elif up_v > 0 and down_v > 0:
        agreement = "conflicted"
        composite = path_lean or None  # price path wins ties
        confidence = "low"
    else:
        agreement = "no_lean"
        composite = None
        confidence = "none"

    return {
        "agreement": agreement,
        "composite_lean": composite,
        "confidence": confidence,
        "votes": votes,
        "active_sources": sources,
    }


def _confirm_fade_for_side(side: Optional[str], div: dict, path_lean: Optional[str]) -> dict:
    """Per-side confirm/fade from divergence overlay + path alignment."""
    side_l = str(side or "").lower()
    cf = (div.get("confirm_fade_by_side") or {})
    out = {"side": side_l or None}
    if side_l in ("up", "down"):
        out["divergence_overlay"] = cf.get(side_l)
        out["proposed"] = cf.get("proposed") or cf.get(side_l)
    path_l = str(path_lean or "").lower()
    if side_l in ("up", "down") and path_l in ("up", "down"):
        out["path_aligned"] = path_l == side_l
    return out


def interpret_tv_for_window(
    *,
    window_seconds: int,
    series_label: str = "",
    tv_chart_lane: Optional[dict] = None,
    price_path: Optional[dict] = None,
    rsi_band: Optional[dict] = None,
    rsi_divergence: Optional[dict] = None,
    alert_history: Optional[dict] = None,
    tv_trend: Optional[dict] = None,
    trade_side: Optional[str] = None,
) -> dict:
    """Synthesize all TV FIFOs into one structured interpretation for Grok/bot."""
    ws = int(window_seconds or 300)
    lane_meta = tv_chart_lane or {}
    lane = lane_meta.get("lane") or tv_lane_kind(window_seconds=ws, series_label=series_label)

    path = _extract_path_lean(price_path)
    band = _extract_band_lean(rsi_band)
    div = _extract_div_lean(rsi_divergence)
    agreement = _score_signal_agreement(
        path_lean=path.get("lean"),
        band_lean=band.get("lean"),
        div_lean=div.get("lean") if div.get("has_signal") else None,
    )

    hist_focus = (alert_history or {}).get("focus") or {}
    hist_trend = hist_focus.get("trend") or {}

    mtf_confirm = (tv_trend or {}).get("confirm_mtf")
    trend_builds = (tv_trend or {}).get("trend_builds")

    # Cardwell context hint when divergence opposes price path
    cardwell_hint = None
    if div.get("has_signal") and path.get("lean"):
        div_l = str(div.get("lean") or "").lower()
        path_l = str(path.get("lean") or "").lower()
        if div_l and path_l and div_l != path_l:
            cardwell_hint = (
                "Divergence opposes price path — Cardwell: may be brief correction within "
                "trend, not full reversal. Prefer price_path lean; use divergence to size down."
            )
        elif div_l == path_l:
            cardwell_hint = "Divergence confirms price path — stronger confirm/fade signal."

    grok_instructions = [
        "Read guide.decision_hierarchy — price_path short lean is primary.",
        "Plot tradingview_15m_price_path.price_pattern.short_path oldest→newest.",
        "RSI band = zone backdrop (30/70); divergence = sparse confirm/fade overlay.",
        "Use composite_lean + signal_agreement for conviction; size down when conflicted.",
        "Cardwell: divergence in-trend often = correction; positive/negative reversals differ.",
        "Lane routing: never mix *USDT (1h) with INDEX *USD (15m) FIFOs.",
    ]

    return {
        "enabled": True,
        "observe_only": True,
        "guide": TV_ALERT_GUIDE,
        "lane": lane,
        "chart_symbol": lane_meta.get("chart_symbol"),
        "feed": lane_meta.get("feed"),
        "price_path": path,
        "rsi_band": band,
        "rsi_divergence": div,
        "signal_agreement": agreement,
        "composite_lean": agreement.get("composite_lean"),
        "composite_confidence": agreement.get("confidence"),
        "alert_history_trend": {
            "pattern": hist_trend.get("pattern"),
            "streak_dir": hist_trend.get("current_streak_dir"),
            "streak_len": hist_trend.get("current_streak_len"),
            "up_fraction": hist_trend.get("up_fraction"),
        } if hist_trend else None,
        "mtf_confirm": mtf_confirm,
        "trend_builds": trend_builds,
        "cardwell_hint": cardwell_hint,
        "confirm_fade": _confirm_fade_for_side(trade_side, div, path.get("lean")),
        "grok_instructions": grok_instructions,
        "note": (
            "Unified TV read: synthesizes bar-close path + RSI 30/70 band + divergence "
            "overlay per lane-routed FIFO. Training grounded in TradingView official docs."
        ),
    }
