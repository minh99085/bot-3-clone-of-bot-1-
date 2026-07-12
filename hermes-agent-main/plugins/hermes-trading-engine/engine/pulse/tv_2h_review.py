"""2-hour TradingView trend review — observe-only context for hourly directional windows.

Filters recent TV alerts, summarizes direction mix, compares against price movement, and flags
alignment/divergence. Alerts are segmented by hourly phase so the bot can learn distinct roles:

  * pre_band  (0–15m)  — early-arrive: open-regime / price-path trend analysis
  * in_band   (15–45m) — actionable: entry-time confirmation (highest decision weight)
  * post_band (45–60m) — late-arrive: completes the 2h lookback tapestry

Does NOT gate trades unless explicitly wired via pre-trade (Phase 2) or council grading
(Phase 3) env flags — both default OFF. TV remains observe-only.
"""

from __future__ import annotations

from typing import Optional

from engine.pulse.grok_bundle import summarize_alert_trend

# Match LearnedHourlyEntryGate / PULSE_HOURLY_{MIN,MAX}_SECONDS_SINCE_OPEN defaults.
PRE_BAND_END_S = 900.0       # 15m — entry band opens
IN_BAND_END_S = 2700.0       # 45m — entry band closes

# Pre-trade / council weights for segmented alignment (sum = 1.0).
SEGMENT_WEIGHTS = {
    "open_regime": 0.30,       # early-arrive → price-path regime
    "actionable_trend": 0.50,  # in-band → entry confirmation
    "lookback_tail": 0.20,     # full 2h incl. late-arrive → study completeness
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def filter_alerts_in_lookback(alerts: list, *, now: float, lookback_s: float) -> list:
    """Return alerts with received_at >= now - lookback_s, oldest→newest."""
    cutoff = float(now) - float(lookback_s)
    rows = [a for a in (alerts or []) if isinstance(a, dict)]
    return [a for a in rows if float(a.get("received_at") or 0.0) >= cutoff]


def alert_hourly_phase(received_at: float) -> str:
    """Map alert landing time to hourly phase: pre_band | in_band | post_band."""
    sso = float(received_at) % 3600.0
    if sso < PRE_BAND_END_S:
        return "pre_band"
    if sso <= IN_BAND_END_S:
        return "in_band"
    return "post_band"


def segment_alerts_by_phase(alerts: list) -> dict:
    """Split alerts into pre_band / in_band / post_band lists (oldest→newest each)."""
    out = {"pre_band": [], "in_band": [], "post_band": []}
    for a in (alerts or []):
        if not isinstance(a, dict):
            continue
        ra = float(a.get("received_at") or 0.0)
        if ra <= 0:
            continue
        phase = alert_hourly_phase(ra)
        row = dict(a)
        row["hourly_phase"] = phase
        row["seconds_since_open"] = round(ra % 3600.0, 1)
        out[phase].append(row)
    return out


def _trend_direction(pattern: str, up_fraction: Optional[float]) -> str:
    pat = str(pattern or "").lower()
    if pat in ("uptrend", "uptrend_bias"):
        return "up"
    if pat in ("downtrend", "downtrend_bias"):
        return "down"
    if up_fraction is not None:
        uf = float(up_fraction)
        if uf >= 0.6:
            return "up"
        if uf <= 0.4:
            return "down"
    return "mixed"


def _price_direction(delta_pct: Optional[float], *, flat_threshold_pct: float = 0.03) -> Optional[str]:
    if delta_pct is None:
        return None
    d = float(delta_pct)
    if d > flat_threshold_pct:
        return "up"
    if d < -flat_threshold_pct:
        return "down"
    return "flat"


def _alignment_flags(
    trend_dir: str,
    price_dir: Optional[str],
    *,
    pattern: str,
) -> tuple[bool, bool, str]:
    """Return (aligned, divergent, alignment_label)."""
    if price_dir is None or trend_dir == "mixed":
        return False, False, "insufficient"
    if price_dir == "flat":
        return False, False, "flat_price"
    if trend_dir == price_dir:
        return True, False, "aligned"
    if trend_dir in ("up", "down") and price_dir in ("up", "down"):
        return False, True, "divergent"
    return False, False, "mixed"


def _confidence_score(
    *,
    alert_count: int,
    streak_len: int,
    aligned: bool,
    lookback_s: float,
    oldest_age_s: Optional[float],
) -> float:
    """0..1 confidence from sample depth + streak + coverage of lookback window."""
    if alert_count <= 0:
        return 0.0
    depth = _clamp01(alert_count / 12.0)  # ~12 alerts in 2h is strong coverage
    streak = _clamp01(streak_len / 4.0)
    coverage = 1.0
    if oldest_age_s is not None and lookback_s > 0:
        coverage = _clamp01(float(oldest_age_s) / float(lookback_s))
    align_bonus = 0.08 if aligned else 0.0
    return round(_clamp01(0.45 * depth + 0.30 * streak + 0.25 * coverage + align_bonus), 4)


def _price_path(
    alerts: list,
    *,
    oracle_price_now: Optional[float],
    trend: dict,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """Return (price_start, price_end, delta_pct, price_direction)."""
    alert_prices = [float(a["price"]) for a in alerts if a.get("price") is not None]
    price_start = alert_prices[0] if alert_prices else trend.get("price_first")
    price_end = (float(oracle_price_now) if oracle_price_now is not None
                 else (alert_prices[-1] if alert_prices else trend.get("price_last")))
    delta = None
    if price_start is not None and price_end is not None and float(price_start) != 0:
        delta = round(
            (float(price_end) - float(price_start)) / float(price_start) * 100.0, 4)
    price_dir = _price_direction(delta)
    if price_dir is None and trend.get("price_delta_pct") is not None:
        price_dir = _price_direction(trend.get("price_delta_pct"))
        if delta is None:
            delta = trend.get("price_delta_pct")
    return price_start, price_end, delta, price_dir


def _by_timeframe_summary(alerts: list) -> dict:
    """Per-chart-TF trend rollup within an alert set (RSI div ladder observe-only)."""
    buckets: dict[str, list] = {}
    for a in (alerts or []):
        if not isinstance(a, dict):
            continue
        tf = str(a.get("timeframe") or "").strip()
        if not tf:
            continue
        buckets.setdefault(tf, []).append(a)
    out = {}
    for tf in sorted(buckets.keys(), key=lambda x: int(x) if str(x).isdigit() else 9999):
        trend = summarize_alert_trend(buckets[tf])
        out[tf] = {
            "alert_count": trend.get("count", 0),
            "pattern": trend.get("pattern"),
            "up_fraction": trend.get("up_fraction"),
            "current_streak_dir": trend.get("current_streak_dir"),
            "current_streak_len": trend.get("current_streak_len"),
            "last_direction": (str(buckets[tf][-1].get("direction") or "").upper()
                               if buckets[tf] else None),
            "last_signal_level": (buckets[tf][-1].get("signal_level") if buckets[tf] else None),
        }
    return out


def tv_ladder_alignment(per_tf_views: Optional[dict], proposed_side: Optional[str]) -> Optional[float]:
    """Alignment of graded per-TF council views (tv_5m..tv_60m) vs proposed side."""
    if not per_tf_views or not proposed_side:
        return None
    side = str(proposed_side).strip().lower()
    if side not in ("up", "down"):
        return None
    leans = []
    for key, p_up in sorted(per_tf_views.items()):
        if not str(key).startswith("tv_") or p_up is None:
            continue
        try:
            p = float(p_up)
        except (TypeError, ValueError):
            continue
        lean = p - 0.5
        if side == "up":
            leans.append(lean)
        else:
            leans.append(-lean)
    if not leans:
        return None
    avg = sum(leans) / len(leans)
    return round(_clamp01(0.5 + avg), 4)


def _summarize_segment(
    alerts: list,
    *,
    role: str,
    now: float,
    lookback_s: float,
    oracle_price_now: Optional[float],
) -> dict:
    """Build one phase/role summary block."""
    trend = summarize_alert_trend(alerts)
    trend_dir = _trend_direction(trend.get("pattern") or "none", trend.get("up_fraction"))
    price_start, price_end, delta, price_dir = _price_path(
        alerts, oracle_price_now=oracle_price_now, trend=trend)
    aligned, divergent, alignment = _alignment_flags(
        trend_dir, price_dir, pattern=str(trend.get("pattern") or ""))
    oldest_ts = min((float(a.get("received_at") or now) for a in alerts), default=now)
    oldest_age_s = max(0.0, now - oldest_ts) if alerts else 0.0
    conf = _confidence_score(
        alert_count=len(alerts),
        streak_len=int(trend.get("current_streak_len") or 0),
        aligned=aligned,
        lookback_s=lookback_s,
        oldest_age_s=oldest_age_s if alerts else None,
    )
    return {
        "role": role,
        "alert_count": len(alerts),
        "up_count": trend.get("up_count", 0),
        "down_count": trend.get("down_count", 0),
        "flat_count": trend.get("flat_count", 0),
        "up_fraction": trend.get("up_fraction"),
        "pattern": trend.get("pattern"),
        "current_streak_dir": trend.get("current_streak_dir"),
        "current_streak_len": trend.get("current_streak_len"),
        "trend_direction": trend_dir,
        "price_start": price_start,
        "price_end": price_end,
        "price_delta_pct": delta,
        "price_direction": price_dir,
        "aligned": aligned,
        "divergent": divergent,
        "alignment": alignment,
        "confidence": conf,
        "by_timeframe": _by_timeframe_summary(alerts),
    }


def _single_alignment(seg: dict, proposed_side: Optional[str]) -> Optional[float]:
    """Alignment score for one segment vs proposed_side (0..1)."""
    if not proposed_side:
        return None
    n = int(seg.get("alert_count") or 0)
    if n < 1:
        return None
    side = str(proposed_side).strip().lower()
    trend_dir = str(seg.get("trend_direction") or "mixed")
    conf = float(seg.get("confidence") or 0.5)
    aligned = bool(seg.get("aligned"))
    divergent = bool(seg.get("divergent"))
    if trend_dir == "mixed":
        return 0.5
    if side == trend_dir:
        base = 0.55 + 0.45 * conf
        return round(_clamp01(base if aligned else base * 0.75), 4)
    if divergent:
        return round(_clamp01(0.45 - 0.35 * conf), 4)
    return round(_clamp01(0.40 - 0.20 * conf), 4)


def tv_2h_trend_p_up(review: Optional[dict]) -> Optional[float]:
    """Map a 2h review dict to p_up for council grading (graded only by default).

    Prefers actionable (in-band) trend when present; falls back to flat review.
    """
    if not review or review.get("enabled") is False:
        return None
    segs = review.get("segments") or {}
    actionable = segs.get("actionable_trend") or {}
    # Prefer in-band if it has enough alerts; else overall review.
    src = actionable if int(actionable.get("alert_count") or 0) >= 2 else review
    n = int(src.get("alert_count") or 0)
    if n < 2:
        return None
    trend_dir = str(src.get("trend_direction") or "mixed")
    conf = float(src.get("confidence") or 0.5)
    aligned = bool(src.get("aligned"))
    scale = 1.0 if aligned else 0.55
    if trend_dir == "up":
        return round(_clamp01(0.5 + 0.5 * conf * scale), 4)
    if trend_dir == "down":
        return round(_clamp01(0.5 - 0.5 * conf * scale), 4)
    up_frac = src.get("up_fraction")
    if up_frac is not None:
        return round(_clamp01(0.2 + 0.6 * float(up_frac)), 4)
    return 0.5


def segment_alignment_scores(
    review: Optional[dict],
    proposed_side: Optional[str],
) -> Optional[dict]:
    """Per-segment alignment scores for learning / dashboard (observe-only)."""
    if not review or review.get("enabled") is False or not proposed_side:
        return None
    segs = review.get("segments") or {}
    if not segs:
        return None
    out = {}
    for key in ("open_regime", "actionable_trend", "lookback_tail"):
        seg = segs.get(key) or {}
        out[key] = {
            "score": _single_alignment(seg, proposed_side),
            "weight": SEGMENT_WEIGHTS.get(key),
            "alert_count": int(seg.get("alert_count") or 0),
            "trend_direction": seg.get("trend_direction"),
            "aligned": seg.get("aligned"),
        }
    return out


def tv_2h_alignment_score(review: Optional[dict], proposed_side: Optional[str]) -> Optional[float]:
    """Pre-trade component: weighted open_regime / actionable / lookback_tail vs side.

    Weights: open_regime 30% (early price-path), actionable 50% (entry confirm),
    lookback_tail 20% (2h completeness). Missing segments are skipped and remaining
    weights renormalized. Late alerts never veto — they only nudge confidence.
    """
    if not review or review.get("enabled") is False or not proposed_side:
        return None
    segs = review.get("segments") or {}
    if segs:
        num, den = 0.0, 0.0
        for key, wt in SEGMENT_WEIGHTS.items():
            seg = segs.get(key) or {}
            s = _single_alignment(seg, proposed_side)
            if s is None:
                continue
            num += float(wt) * float(s)
            den += float(wt)
        if den > 0:
            return round(num / den, 4)
    # Flat fallback (pre-segment reviews / empty segments)
    n = int(review.get("alert_count") or 0)
    if n < 1:
        return 0.5
    return _single_alignment(review, proposed_side)


def compute_tv_2h_review(
    *,
    alerts: list,
    now: float,
    lookback_s: float = 7200.0,
    symbol: str = "BTCUSD",
    oracle_price_now: Optional[float] = None,
) -> dict:
    """Build the 2h TV trend review block for one symbol (flat + segmented)."""
    now = float(now)
    lookback_s = max(60.0, float(lookback_s))
    window_alerts = filter_alerts_in_lookback(alerts, now=now, lookback_s=lookback_s)
    phases = segment_alerts_by_phase(window_alerts)

    # Flat overall (backward-compatible fields)
    overall = _summarize_segment(
        window_alerts, role="lookback_full", now=now, lookback_s=lookback_s,
        oracle_price_now=oracle_price_now)

    open_regime = _summarize_segment(
        phases["pre_band"], role="open_regime", now=now, lookback_s=lookback_s,
        oracle_price_now=oracle_price_now)
    open_regime["phase"] = "pre_band"
    open_regime["phase_s"] = [0, int(PRE_BAND_END_S)]
    open_regime["note"] = ("Early-arrive alerts (0–15m): open-regime / price-path trend; "
                           "not an entry trigger.")

    actionable = _summarize_segment(
        phases["in_band"], role="actionable_trend", now=now, lookback_s=lookback_s,
        oracle_price_now=oracle_price_now)
    actionable["phase"] = "in_band"
    actionable["phase_s"] = [int(PRE_BAND_END_S), int(IN_BAND_END_S)]
    actionable["note"] = ("In-band alerts (15–45m): entry-time confirmation; "
                          "highest weight for decisions.")

    post = _summarize_segment(
        phases["post_band"], role="post_band_tail", now=now, lookback_s=lookback_s,
        oracle_price_now=oracle_price_now)
    post["phase"] = "post_band"
    post["phase_s"] = [int(IN_BAND_END_S), 3600]
    post["note"] = ("Late-arrive alerts (45–60m): complete prior-hour story for 2h study; "
                    "never veto entry.")

    # lookback_tail = full 2h tapestry (includes late); used for study completeness weight
    lookback_tail = dict(overall)
    lookback_tail["role"] = "lookback_tail"
    lookback_tail["post_band_alert_count"] = len(phases["post_band"])
    lookback_tail["note"] = ("Full 2h lookback incl. late-arrive alerts — study completeness, "
                             "not a hard gate.")

    oldest_ts = min((float(a.get("received_at") or now) for a in window_alerts), default=now)
    oldest_age_s = max(0.0, now - oldest_ts)

    return {
        "enabled": True,
        "observe_only": True,
        "symbol": str(symbol or "BTCUSD"),
        "lookback_s": int(lookback_s),
        "window_start_ts": round(now - lookback_s, 3),
        "window_end_ts": round(now, 3),
        "entry_band_s": [int(PRE_BAND_END_S), int(IN_BAND_END_S)],
        "segment_weights": dict(SEGMENT_WEIGHTS),
        # Flat fields (backward compatible — overall 2h mix)
        "alert_count": overall["alert_count"],
        "up_count": overall["up_count"],
        "down_count": overall["down_count"],
        "flat_count": overall["flat_count"],
        "up_fraction": overall["up_fraction"],
        "pattern": overall["pattern"],
        "current_streak_dir": overall["current_streak_dir"],
        "current_streak_len": overall["current_streak_len"],
        "trend_direction": overall["trend_direction"],
        "price_start": overall["price_start"],
        "price_end": overall["price_end"],
        "oracle_price_now": oracle_price_now,
        "price_delta_pct": overall["price_delta_pct"],
        "price_direction": overall["price_direction"],
        "aligned": overall["aligned"],
        "divergent": overall["divergent"],
        "alignment": overall["alignment"],
        "confidence": overall["confidence"],
        "coverage_s": round(oldest_age_s, 1),
        "phase_counts": {
            "pre_band": len(phases["pre_band"]),
            "in_band": len(phases["in_band"]),
            "post_band": len(phases["post_band"]),
        },
        "by_timeframe": _by_timeframe_summary(window_alerts),
        "segments": {
            "open_regime": open_regime,
            "actionable_trend": actionable,
            "lookback_tail": lookback_tail,
            "post_band": post,
        },
        "note": ("2h TV alert trend vs price path — segmented by hourly phase "
                 "(early=regime, in-band=decision, late=2h study). Observe-only; "
                 "not a trade gate unless pretrade/council flags are enabled."),
    }
