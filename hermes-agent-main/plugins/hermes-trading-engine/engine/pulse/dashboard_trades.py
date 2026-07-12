"""Unified recent-trade rows for the read-only pulse dashboard (hourly + 15m directional)."""
from __future__ import annotations

from typing import Any


def _sort_ts(row: dict) -> float:
    for key in ("sort_ts", "close_ts", "entry_ts", "open_ts"):
        val = row.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return 0.0


def _format_ttm_seconds(sec: float | None) -> str | None:
    """Human-readable time-to-maturity (seconds remaining at entry)."""
    if sec is None:
        return None
    try:
        s = max(0, int(round(float(sec))))
    except (TypeError, ValueError):
        return None
    if s < 60:
        return "%ds" % s
    mins, rem = divmod(s, 60)
    if mins < 60:
        return ("%dm %02ds" % (mins, rem)) if rem else ("%dm" % mins)
    hrs, mins = divmod(mins, 60)
    return ("%dh %02dm" % (hrs, mins)) if mins else ("%dh" % hrs)


def _ttm_fields(pos: dict) -> dict:
    """TTM at entry from research.entry_ttc_s or close_ts - entry_ts fallback."""
    res = pos.get("research") or {}
    ttm = res.get("entry_ttc_s")
    if ttm is None and pos.get("entry_ts") is not None and pos.get("close_ts") is not None:
        try:
            ttm = float(pos["close_ts"]) - float(pos["entry_ts"])
        except (TypeError, ValueError):
            ttm = None
    if ttm is None and res.get("open_ts") is not None and pos.get("entry_ts") is not None:
        ws = res.get("window_seconds")
        if ws is not None:
            try:
                ttm = float(ws) - (float(pos["entry_ts"]) - float(res["open_ts"]))
            except (TypeError, ValueError):
                ttm = None
    if ttm is None:
        return {}
    try:
        ttm_f = max(0.0, float(ttm))
    except (TypeError, ValueError):
        return {}
    label = _format_ttm_seconds(ttm_f)
    if not label:
        return {}
    return {"ttm_s": round(ttm_f, 1), "ttm_label": label}


def _lane_realized_pnl(positions: list) -> float | None:
    total = 0.0
    n = 0
    for pos in positions:
        if not isinstance(pos, dict) or str(pos.get("status") or "") != "settled":
            continue
        pnl = pos.get("pnl_usd")
        if pnl is None:
            pnl = pos.get("realized_profit_usd")
        if pnl is None:
            continue
        total += float(pnl)
        n += 1
    return round(total, 2) if n else None


def _directional_row(pos: dict) -> dict:
    row = dict(pos)
    row.setdefault("trade_type", "directional")
    row["sort_ts"] = _sort_ts(row)
    res = dict(row.get("research") or {})
    res.setdefault("series_label", "directional")
    if not res.get("market_series"):
        res["market_series"] = str(row.get("title") or res.get("series_label") or "directional")[:40]
    from engine.pulse.directional_labels import labels_from_research
    labels = labels_from_research(res, title=str(row.get("title") or ""))
    row["trade_symbol"] = labels["trade_symbol"]
    row["market_tf"] = labels["market_tf"]
    row["market_kind_label"] = labels["market_kind_label"]
    if labels.get("window_entry_label"):
        row["window_entry_at"] = labels.get("window_entry_at")
        row["window_entry_label"] = labels.get("window_entry_label")
        row["window_entry_min"] = labels.get("window_entry_min")
        row["hourly_entry_bucket"] = labels.get("hourly_entry_bucket")
    ext = row.get("external") or {}
    if ext.get("timeframe"):
        row["tv_timeframe"] = str(ext.get("timeframe"))
    elif res.get("tv_timeframe"):
        row["tv_timeframe"] = str(res.get("tv_timeframe"))
    row["research"] = res
    _s = str(row.get("side") or "").lower()
    if _s in ("up", "down"):
        row["side"] = _s.upper()
    row.update(_ttm_fields(row))
    return row


def _position_counts(positions: list) -> dict[str, Any]:
    wins = losses = open_n = settled = 0
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        status = str(pos.get("status") or "")
        if status == "open":
            open_n += 1
        if status != "settled":
            continue
        settled += 1
        if pos.get("won") is not None:
            if bool(pos.get("won")):
                wins += 1
            else:
                losses += 1
        elif pos.get("pnl_usd") is not None:
            pnl = float(pos.get("pnl_usd"))
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
    total = len([p for p in positions if isinstance(p, dict)])
    wr = (wins / settled) if settled else None
    return {"total": total, "wins": wins, "losses": losses, "open": open_n,
            "settled": settled, "win_rate": round(wr, 4) if wr is not None else None}


def directional_stats(ledger: dict | None) -> dict[str, Any]:
    """Hourly directional lane counts."""
    empty = {"total": 0, "wins": 0, "losses": 0, "open": 0, "settled": 0, "win_rate": None,
             "realized_pnl_usd": None}
    if not ledger:
        return empty
    positions = [p for p in (ledger.get("positions") or []) if isinstance(p, dict)]
    out = _position_counts(positions)
    out["realized_pnl_usd"] = _lane_realized_pnl(positions)
    stats = ledger.get("stats") or {}
    if stats.get("trades") is not None and out["total"] == 0:
        settled = int(stats.get("settled") or stats.get("trades") or 0)
        wins = int(stats.get("wins") or 0)
        out = {
            "total": settled + int(stats.get("open_positions") or 0),
            "wins": wins,
            "losses": max(0, settled - wins),
            "open": int(stats.get("open_positions") or 0),
            "settled": settled,
            "win_rate": stats.get("win_rate"),
            "realized_pnl_usd": stats.get("realized_pnl_usd"),
        }
    return {**empty, **out}


def lane_stats(ledger: dict | None) -> dict[str, Any]:
    """Per-asset + per-timeframe trade counts for the dashboard."""
    return {
        "btc": symbol_stats(ledger, "BTC"),
        "eth": symbol_stats(ledger, "ETH"),
        "btc_1h": symbol_stats(ledger, "BTC", market_tf="1h"),
        "btc_15m": symbol_stats(ledger, "BTC", market_tf="15m"),
        "eth_1h": symbol_stats(ledger, "ETH", market_tf="1h"),
        "eth_15m": symbol_stats(ledger, "ETH", market_tf="15m"),
        "directional": directional_stats(ledger),
    }


def _position_symbol(pos: dict) -> str | None:
    """Resolve trade symbol for a raw ledger position."""
    from engine.pulse.directional_labels import labels_from_research

    res = pos.get("research") or {}
    labels = labels_from_research(res, title=str(pos.get("title") or ""))
    sym = str(labels.get("trade_symbol") or "").strip().upper()
    return sym if sym and sym != "—" else None


def _position_market_tf(pos: dict) -> str:
    from engine.pulse.directional_labels import labels_from_research

    res = pos.get("research") or {}
    labels = labels_from_research(res, title=str(pos.get("title") or ""))
    return str(labels.get("market_tf") or "—")


def _positions_for_symbol_tf(
    ledger: dict | None, symbol: str, market_tf: str | None = None,
) -> list[dict]:
    if not ledger:
        return []
    want = str(symbol or "").strip().upper()
    if not want:
        return []
    tf_want = str(market_tf or "").strip().lower() if market_tf else None
    out: list[dict] = []
    for pos in ledger.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        if _position_symbol(pos) != want:
            continue
        if tf_want and _position_market_tf(pos).lower() != tf_want:
            continue
        out.append(pos)
    return out


def _positions_for_symbol(ledger: dict | None, symbol: str) -> list[dict]:
    return _positions_for_symbol_tf(ledger, symbol, market_tf=None)


def symbol_stats(ledger: dict | None, symbol: str, *, market_tf: str | None = None) -> dict[str, Any]:
    """Per-asset directional lane counts (optional 1h / 15m filter)."""
    empty = {"total": 0, "wins": 0, "losses": 0, "open": 0, "settled": 0, "win_rate": None,
             "realized_pnl_usd": None}
    positions = _positions_for_symbol_tf(ledger, symbol, market_tf=market_tf)
    if not positions:
        return empty
    out = _position_counts(positions)
    out["realized_pnl_usd"] = _lane_realized_pnl(positions)
    return {**empty, **out}


def symbol_trades_for_dashboard(
    ledger: dict | None, *, symbol: str, limit: int = 50, market_tf: str | None = None,
) -> list[dict]:
    """Directional positions for one asset (optional timeframe), newest first."""
    rows = [_directional_row(pos) for pos in _positions_for_symbol_tf(
        ledger, symbol, market_tf=market_tf)]
    rows.sort(key=_sort_ts, reverse=True)
    return rows[: max(1, int(limit))]


def directional_trades_for_dashboard(ledger: dict | None, *, limit: int = 50) -> list[dict]:
    """Directional positions, newest first."""
    if not ledger:
        return []
    rows: list[dict] = []
    for pos in ledger.get("positions") or []:
        if isinstance(pos, dict):
            rows.append(_directional_row(pos))
    rows.sort(key=_sort_ts, reverse=True)
    return rows[: max(1, int(limit))]


def lane_trades_for_dashboard(ledger: dict | None, *, limit: int = 50) -> dict[str, list[dict]]:
    """Recent trades for the dashboard (up to ``limit``, newest first)."""
    lim = max(1, int(limit))
    return {
        "btc": symbol_trades_for_dashboard(ledger, symbol="BTC", limit=lim),
        "eth": symbol_trades_for_dashboard(ledger, symbol="ETH", limit=lim),
        "btc_1h": symbol_trades_for_dashboard(ledger, symbol="BTC", limit=lim, market_tf="1h"),
        "btc_15m": symbol_trades_for_dashboard(ledger, symbol="BTC", limit=lim, market_tf="15m"),
        "eth_1h": symbol_trades_for_dashboard(ledger, symbol="ETH", limit=lim, market_tf="1h"),
        "eth_15m": symbol_trades_for_dashboard(ledger, symbol="ETH", limit=lim, market_tf="15m"),
        "directional": directional_trades_for_dashboard(ledger, limit=lim),
    }


def recent_trades_for_dashboard(ledger: dict | None, *, limit: int = 20) -> list[dict]:
    """Merge directional trades for the dashboard sidebar."""
    return directional_trades_for_dashboard(ledger, limit=limit)
