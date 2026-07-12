"""TV bar-close price-path — dual-horizon chart (5m primary, 15m fallback).

4-chart setup: BTC/ETH × BarClose5m (+ separate RSI overlay FIFO).
Bot stores last ``regime_n`` bar-close alerts/symbol (default 50).
Trading lean uses last ``short_n`` (default 8 ≈ 40m of 5m bars).

  * regime (50)  — structure / context for Grok narrative
  * short  (6–12) — short-term trend for entry lean + size bias

RSI divergence is NEVER mixed into this path — see ``tv_rsi_overlay``.
Settlement truth remains Chainlink.
"""

from __future__ import annotations

from typing import Optional

from engine.pulse.grok_bundle import summarize_alert_trend

# Defaults: 50 × 5m ≈ ~4h regime; 8 × 5m ≈ ~40m short-term trade lean.
DEFAULT_REGIME_N = 50
DEFAULT_SHORT_N = 8
MIN_SHORT_N = 6


def _f(x) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def filter_bar_close(alerts: list, *, prefer_bar_close: bool = True) -> list:
    """Prefer bar_close_5m, then bar_close_15m / BAR_*; never include rsi_divergence."""
    rows = [a for a in (alerts or []) if isinstance(a, dict)]
    if not rows:
        return []
    bar5, bar15, tf5, tf15 = [], [], [], []
    for a in rows:
        kind = str(a.get("signal_kind") or "").strip().lower()
        level = str(a.get("signal_level") or "").strip().upper()
        tf = str(a.get("timeframe") or "").strip()
        if kind == "rsi_divergence" or "DIV" in level:
            continue
        if kind == "bar_close_5m":
            bar5.append(a)
        elif kind == "bar_close_15m" or level in ("BAR_BULL", "BAR_BEAR"):
            bar15.append(a)
        elif tf in ("5", "5m", "5M"):
            tf5.append(a)
        elif tf in ("15", "15m", "15M"):
            tf15.append(a)
    if prefer_bar_close:
        if bar5:
            return bar5
        if bar15:
            return bar15
    return bar5 or bar15 or tf5 or tf15 or []


def filter_bar_close_15m(alerts: list, *, prefer_bar_close: bool = True) -> list:
    """Back-compat alias — prefers 5m bar-close when present."""
    return filter_bar_close(alerts, prefer_bar_close=prefer_bar_close)


def filter_bar_close_5m(alerts: list, *, prefer_bar_close: bool = True) -> list:
    return filter_bar_close(alerts, prefer_bar_close=prefer_bar_close)


def path_symbol_candidates(symbol: Optional[str], *, strict_lane: bool = True) -> list[str]:
    """Return chart symbols to read for bar-close / RSI FIFOs.

    ``strict_lane=True`` (default): only the lane-routed symbol — 1h *USDT vs 15m INDEX *USD
    never cross-feed. ``strict_lane=False`` retains legacy dual fallback for migration tests.
    """
    s = str(symbol or "").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    if not s:
        return ["BTCUSD"] if strict_lane else ["BTCUSDT", "BTCUSD"]
    if strict_lane:
        return [s]
    if s.startswith("BTC"):
        return ["BTCUSDT", "BTCUSD"]
    if s.startswith("ETH"):
        return ["ETHUSDT", "ETHUSD"]
    return [s]


def _bar_close_feed_score(rows: list) -> float:
    """Prefer fresher bar_close_5m FIFOs over stale 15m / mixed history."""
    filtered = filter_bar_close(rows)
    if not filtered:
        return -1.0
    n5 = sum(1 for r in filtered
             if str(r.get("signal_kind") or "").strip().lower() == "bar_close_5m")
    last_t = 0.0
    for r in filtered:
        try:
            t = float(r.get("received_at") or r.get("bar_time") or 0.0)
        except (TypeError, ValueError):
            continue
        if t > 1e12:
            t /= 1000.0
        if t > last_t:
            last_t = t
    return float(n5) * 1e12 + last_t + float(len(filtered))


def resolve_bar_close_history(history_by_symbol: dict, symbol: Optional[str],
                              *, strict_lane: bool = True) -> tuple[str, list]:
    """Resolve bar-close FIFO for the lane-routed symbol (no USDT/USD mixing by default)."""
    by = history_by_symbol or {}
    cands = path_symbol_candidates(symbol, strict_lane=strict_lane)
    sym = cands[0] if cands else str(symbol or "BTCUSD").strip().upper() or "BTCUSD"
    rows = list(by.get(sym) or []) if isinstance(by.get(sym), list) else []
    if not rows and not strict_lane:
        best_sym, best_rows, best_score = sym, rows, _bar_close_feed_score(rows)
        for cand in path_symbol_candidates(symbol, strict_lane=False):
            alt = by.get(cand) or []
            if not isinstance(alt, list):
                continue
            score = _bar_close_feed_score(alt)
            if score > best_score:
                best_sym, best_rows, best_score = cand, list(alt), score
        return best_sym, filter_bar_close(best_rows)
    return sym, filter_bar_close(rows)


def resolve_bar_close_from_intake(intake, symbol: Optional[str],
                                  *, strict_lane: bool = True) -> tuple[str, list]:
    """Same as resolve_bar_close_history but via TradingViewIntake methods."""
    if intake is None:
        cands = path_symbol_candidates(symbol, strict_lane=strict_lane)
        return (cands[0] if cands else str(symbol or "BTCUSD")), []
    by = {}
    for cand in path_symbol_candidates(symbol, strict_lane=strict_lane):
        try:
            by[cand] = list(intake.alert_history_for_symbol(cand) or [])
        except Exception:  # noqa: BLE001
            by[cand] = []
    return resolve_bar_close_history(by, symbol, strict_lane=strict_lane)


def compact_path_for_plot(dual: Optional[dict], *, max_short: int = 8,
                          max_regime: int = 12) -> dict:
    """Compact OHLC pattern block for Grok-MC / decider (plot short + regime summary)."""
    d = dual or {}
    short = d.get("short_term") or {}
    regime = d.get("regime") or {}
    sp = list(short.get("path") or [])[-int(max_short):]
    rp = list(regime.get("path") or [])[-int(max_regime):]

    def _pts(path: list) -> list:
        out = []
        for p in path:
            out.append({
                "t": p.get("t"),
                "o": p.get("open"),
                "h": p.get("high"),
                "l": p.get("low"),
                "c": p.get("close") if p.get("close") is not None else p.get("price"),
                "d": p.get("direction"),
            })
        return out

    return {
        "trade_lean": d.get("trade_lean") or d.get("lean"),
        "alignment": d.get("alignment"),
        "confidence": d.get("confidence"),
        "short_pattern": (short.get("trend") or {}).get("pattern"),
        "regime_pattern": (regime.get("trend") or {}).get("pattern"),
        "short_delta_pct": short.get("price_delta_pct"),
        "regime_delta_pct": regime.get("price_delta_pct"),
        "short_path": _pts(sp),
        "regime_path_tail": _pts(rp),
        "source": d.get("source"),
        "note": "Plot short_path (oldest→newest OHLC) as the current price pattern; "
                "regime_path_tail is HTF context only. RSI is separate overlay.",
    }


def build_price_path(alerts: list, *, max_points: int = 50) -> list:
    """Oldest→newest compact OHLC points for Grok to 'plot' the move."""
    rows = [a for a in (alerts or []) if isinstance(a, dict)]
    if max_points > 0:
        rows = rows[-int(max_points):]
    out = []
    for a in rows:
        px = _f(a.get("close") if a.get("close") is not None else a.get("price"))
        out.append({
            "t": a.get("bar_time") or a.get("received_at"),
            "direction": str(a.get("direction") or "").upper() or None,
            "open": _f(a.get("open")),
            "high": _f(a.get("high")),
            "low": _f(a.get("low")),
            "close": px,
            "price": px,
            "body_pct": _f(a.get("body_pct")),
            "body_ratio": _f(a.get("body_ratio")),
            "streak": a.get("streak"),
            "strength": _f(a.get("strength")),
            "signal_level": a.get("signal_level"),
        })
    return out


def _trend_block(path: list, *, max_points: int, role: str) -> dict:
    trend_rows = [{"direction": p.get("direction"), "price": p.get("price")} for p in path]
    trend = summarize_alert_trend(trend_rows)
    first = path[0]["price"] if path else None
    last = path[-1]["price"] if path else None
    delta = None
    if first is not None and last is not None and float(first) != 0:
        delta = round((float(last) - float(first)) / float(first) * 100.0, 4)
    pattern = str(trend.get("pattern") or "none")
    streak_dir = trend.get("current_streak_dir")
    up_frac = trend.get("up_fraction")
    # Short-term lean: prefer ending streak / pattern (current move).
    # Regime lean: prefer overall path (price delta + up_fraction) so a late
    # bounce does not flip the HTF structure read.
    lean = None
    if role == "regime":
        dlt = delta if delta is not None else trend.get("price_delta_pct")
        if dlt is not None and abs(float(dlt)) >= 0.15:
            lean = "up" if float(dlt) > 0 else "down"
        elif up_frac is not None:
            if float(up_frac) >= 0.6:
                lean = "up"
            elif float(up_frac) <= 0.4:
                lean = "down"
        elif pattern in ("uptrend", "uptrend_bias"):
            lean = "up"
        elif pattern in ("downtrend", "downtrend_bias"):
            lean = "down"
    else:
        if pattern in ("uptrend", "uptrend_bias") or streak_dir == "UP":
            lean = "up"
        elif pattern in ("downtrend", "downtrend_bias") or streak_dir == "DOWN":
            lean = "down"
    return {
        "role": role,
        "n": len(path),
        "max_points": int(max_points),
        "source": "tv_bar_close_15m",
        "observe_only": True,
        "trend": trend,
        "lean": lean,
        "price_first": first,
        "price_last": last,
        "price_delta_pct": delta if delta is not None else trend.get("price_delta_pct"),
        "path": path,
    }


def price_path_trend(alerts: list, *, max_points: int = 50) -> dict:
    """Single-horizon trend (legacy helper). Prefer :func:`dual_horizon_price_path`."""
    filtered = filter_bar_close(alerts)
    path = build_price_path(filtered, max_points=max_points)
    out = _trend_block(path, max_points=max_points, role="single")
    out["note"] = (
        "Last %d TV 15m bar-close alerts as OHLC path — Grok/bot learn price "
        "movement trend; FIFO drops oldest when full. Observe-only." % int(max_points))
    return out


def dual_horizon_price_path(
    alerts: list,
    *,
    regime_n: int = DEFAULT_REGIME_N,
    short_n: int = DEFAULT_SHORT_N,
) -> dict:
    """Regime (50) + short-term (6–8) paths from the same FIFO.

    Returns alignment between horizons so the bot can trade short-term lean when
    it agrees with regime, or stay cautious when they diverge.
    """
    regime_n = max(1, int(regime_n or DEFAULT_REGIME_N))
    short_n = max(MIN_SHORT_N, min(int(short_n or DEFAULT_SHORT_N), regime_n))
    filtered = filter_bar_close(alerts)
    regime_path = build_price_path(filtered, max_points=regime_n)
    short_path = build_price_path(filtered, max_points=short_n)
    regime = _trend_block(regime_path, max_points=regime_n, role="regime")
    short = _trend_block(short_path, max_points=short_n, role="short_term")

    r_lean = regime.get("lean")
    s_lean = short.get("lean")
    if s_lean and r_lean and s_lean == r_lean:
        alignment = "aligned"
        trade_lean = s_lean
        confidence = "high"
    elif s_lean and r_lean and s_lean != r_lean:
        alignment = "divergent"
        trade_lean = s_lean  # short drives entry; regime warns
        confidence = "low"
    elif s_lean:
        alignment = "short_only"
        trade_lean = s_lean
        confidence = "medium"
    elif r_lean:
        alignment = "regime_only"
        trade_lean = None  # do not trade on regime alone
        confidence = "low"
    else:
        alignment = "none"
        trade_lean = None
        confidence = "none"

    return {
        "source": "tv_bar_close_dual",
        "observe_only": True,
        "regime_n": regime_n,
        "short_n": short_n,
        "regime": regime,
        "short_term": short,
        # Back-compat: focus.path/trend == regime (HTF chart)
        "n": regime.get("n"),
        "max_points": regime_n,
        "trend": regime.get("trend"),
        "lean": trade_lean,
        "price_first": regime.get("price_first"),
        "price_last": regime.get("price_last"),
        "price_delta_pct": regime.get("price_delta_pct"),
        "path": regime.get("path"),
        "alignment": alignment,
        "trade_lean": trade_lean,
        "confidence": confidence,
        "note": (
            "Dual-horizon OHLC from bar-close FIFO (5m preferred): "
            "regime=last %d; short_term=last %d. RSI never mixed in."
            % (regime_n, short_n)
        ),
    }


def trade_lean_from_path(dual: Optional[dict]) -> dict:
    """Compact lean for lane filters / research tags."""
    d = dual or {}
    short = d.get("short_term") or {}
    regime = d.get("regime") or {}
    return {
        "trade_lean": d.get("trade_lean") or d.get("lean"),
        "alignment": d.get("alignment") or "none",
        "confidence": d.get("confidence") or "none",
        "short_pattern": (short.get("trend") or {}).get("pattern"),
        "short_streak_dir": (short.get("trend") or {}).get("current_streak_dir"),
        "short_streak_len": (short.get("trend") or {}).get("current_streak_len"),
        "short_n": short.get("n"),
        "regime_pattern": (regime.get("trend") or {}).get("pattern"),
        "regime_lean": regime.get("lean"),
        "regime_n": regime.get("n"),
        "price_delta_pct_short": short.get("price_delta_pct"),
        "price_delta_pct_regime": regime.get("price_delta_pct"),
    }


def hourly_chart_lean_entry_ok(
    *,
    side: Optional[str],
    lean: Optional[dict],
    seconds_since_open: float,
    min_short_n: int = MIN_SHORT_N,
    min_sso_s: float = 900.0,
    gate_enabled: bool = True,
) -> tuple[bool, str]:
    """Hard 1h entry gate from last-N bar-close short-term lean.

    Blocks entries before ``min_sso_s`` (default 15m = one 15m bar into the hour)
    and when the last ``min_short_n`` (default 6) bar-close alerts oppose the side.
    """
    if not gate_enabled:
        return True, "gate_disabled"
    side_l = str(side or "").lower()
    if side_l not in ("up", "down"):
        return True, "no_side"
    sso = float(seconds_since_open or 0)
    if sso < float(min_sso_s):
        return False, "hourly_chart_lean_too_early"
    lean = lean or {}
    short_n = int(lean.get("short_n") or 0)
    if short_n < int(min_short_n):
        return False, "hourly_chart_lean_cold"
    trade_lean = str(lean.get("trade_lean") or "").lower()
    if trade_lean not in ("up", "down"):
        return False, "hourly_chart_lean_no_read"
    if trade_lean != side_l:
        return False, "hourly_chart_lean_opposed"
    return True, "ok"


def size_mult_for_lean(*, side: Optional[str], lean: Optional[dict],
                       aligned_mult: float = 1.15,
                       divergent_mult: float = 0.55,
                       oppose_mult: float = 0.35) -> float:
    """Soft size bias from short-term chart lean (never forces a trade)."""
    if not side or not lean:
        return 1.0
    trade_lean = str(lean.get("trade_lean") or "").lower()
    if trade_lean not in ("up", "down"):
        return 1.0
    side = str(side).lower()
    alignment = str(lean.get("alignment") or "")
    agrees = (trade_lean == side)
    if agrees and alignment == "aligned":
        return float(aligned_mult)
    if agrees and alignment in ("short_only", "divergent"):
        return 1.0 if alignment == "short_only" else 0.75
    if not agrees and alignment == "aligned":
        return float(oppose_mult)
    if not agrees:
        return float(divergent_mult)
    return 1.0


def tv_15m_price_path_snapshot(
    *,
    history: Optional[dict],
    focus_symbol: str = "BTCUSD",
    max_points: int = DEFAULT_REGIME_N,
    short_n: int = DEFAULT_SHORT_N,
) -> dict:
    """Per-symbol dual-horizon paths for the Grok decision bundle + status."""
    hist = history or {}
    requested = str(focus_symbol or "BTCUSD").strip() or "BTCUSD"
    regime_n = max(1, int(max_points or DEFAULT_REGIME_N))
    short = max(MIN_SHORT_N, min(int(short_n or DEFAULT_SHORT_N), regime_n))
    by_symbol_raw = hist.get("by_symbol") or {}
    by_symbol: dict = {}
    for sym in sorted(by_symbol_raw.keys()):
        rows = [r for r in (by_symbol_raw.get(sym) or []) if isinstance(r, dict)]
        if not rows:
            continue
        block = dual_horizon_price_path(rows, regime_n=regime_n, short_n=short)
        block["symbol"] = sym
        by_symbol[sym] = block
    # Prefer *USDT 5m bar-close when present (4-chart setup).
    focus, focus_rows = resolve_bar_close_history(by_symbol_raw, requested)
    focus_block = by_symbol.get(focus)
    if focus_block is None and focus_rows:
        focus_block = dual_horizon_price_path(focus_rows, regime_n=regime_n, short_n=short)
        focus_block["symbol"] = focus
    plot = compact_path_for_plot(focus_block)
    return {
        "max_points": regime_n,
        "regime_n": regime_n,
        "short_n": short,
        "focus_symbol": focus,
        "requested_symbol": requested,
        "focus": focus_block,
        "price_pattern": plot,
        "by_symbol": by_symbol,
        "observe_only": True,
        "note": (
            "Rolling FIFO last %d alerts/symbol (5m bar-close preferred on *USDT): "
            "regime chart for Grok; last %d = short-term trade lean / price pattern."
            % (regime_n, short)
        ),
    }
