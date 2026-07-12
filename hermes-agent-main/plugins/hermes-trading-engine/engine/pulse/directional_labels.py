"""Human-readable asset + market-timeframe labels for directional trades."""
from __future__ import annotations

_TITLE_ASSETS = (
    ("bitcoin", "BTC"),
    ("ethereum", "ETH"),
    ("solana", "SOL"),
    ("xrp", "XRP"),
    ("doge", "DOGE"),
    ("dogecoin", "DOGE"),
    ("bnb", "BNB"),
    ("binance coin", "BNB"),
)


def asset_from_title(title: str) -> str | None:
    t = str(title or "").lower()
    for needle, sym in _TITLE_ASSETS:
        if needle in t:
            return sym
    return None


def market_tf_from_window(*, series_label: str = "", window_seconds: int | None = None) -> str:
    sl = str(series_label or "").strip().lower()
    if window_seconds is not None:
        ws = int(window_seconds)
        if ws >= 3600:
            return "1h"
        if ws >= 900:
            return "15m"
        if ws >= 300:
            return "5m"
    if sl in ("5m", "15m"):
        return sl
    if sl.endswith("_1h") or sl.endswith("_above") or sl == "dir_1h":
        return "1h"
    if sl.endswith("_4h"):
        return "4h"
    if sl.endswith("_1d"):
        return "1d"
    return sl or "—"


def asset_from_series_label(series_label: str) -> str | None:
    sl = str(series_label or "").strip().lower()
    if not sl:
        return None
    if sl in ("5m", "15m"):
        return "BTC"
    for prefix, sym in (("btc", "BTC"), ("eth", "ETH"), ("sol", "SOL"),
                        ("xrp", "XRP"), ("doge", "DOGE"), ("bnb", "BNB")):
        if sl.startswith(prefix):
            return sym
    return None


def directional_trade_labels(
    *,
    title: str = "",
    series_label: str = "",
    series_slug: str = "",
    slug: str = "",
    window_seconds: int | None = None,
    market_kind: str = "",
) -> dict:
    """Return trade_symbol, market_tf, market_kind_label for dashboard + ledger research."""
    sym = asset_from_title(title) or asset_from_series_label(series_label)
    if sym is None and slug:
        sym = asset_from_title(slug)
    if sym is None and series_slug:
        low = series_slug.lower()
        if "eth" in low:
            sym = "ETH"
        elif "btc" in low or "bitcoin" in low:
            sym = "BTC"
        elif "sol" in low:
            sym = "SOL"
    tf = market_tf_from_window(series_label=series_label, window_seconds=window_seconds)
    kind = str(market_kind or "").strip().lower() or "updown"
    kind_label = "above strike" if kind == "above" else "up/down"
    return {
        "trade_symbol": sym or "—",
        "market_tf": tf,
        "market_kind_label": kind_label,
    }


def labels_from_research(research: dict | None, *, title: str = "") -> dict:
    """Backfill labels for older positions that predate trade_symbol storage."""
    r = research or {}
    if r.get("trade_symbol") and r.get("market_tf"):
        out = {
            "trade_symbol": r["trade_symbol"],
            "market_tf": r["market_tf"],
            "market_kind_label": r.get("market_kind_label") or "up/down",
        }
        out.update(window_entry_timing_from_position(
            open_ts=r.get("open_ts"),
            entry_ts=r.get("entry_ts"),
            seconds_since_open=r.get("seconds_since_open_at_entry"),
            window_seconds=r.get("window_seconds"),
            series_label=str(r.get("series_label") or r.get("market_series") or ""),
            hourly_entry_bucket=str(r.get("hourly_entry_bucket") or ""),
        ))
        return out
    base = directional_trade_labels(
        title=title or r.get("directional_slug") or "",
        series_label=str(r.get("series_label") or r.get("market_series") or ""),
        series_slug=str(r.get("series_slug") or ""),
        slug=str(r.get("directional_slug") or ""),
        window_seconds=r.get("window_seconds"),
        market_kind=str(r.get("market_kind") or ""),
    )
    base.update(window_entry_timing_from_position(
        open_ts=r.get("open_ts"),
        entry_ts=r.get("entry_ts"),
        seconds_since_open=r.get("seconds_since_open_at_entry"),
        window_seconds=r.get("window_seconds"),
        series_label=str(r.get("series_label") or r.get("market_series") or ""),
        hourly_entry_bucket=str(r.get("hourly_entry_bucket") or ""),
    ))
    return base


_HOURLY_BUCKET_LABEL = {
    "h0_5m": "0-5m band",
    "h5_15m": "5-15m band",
    "h15_30m": "15-30m band",
    "h30_45m": "30-45m band",
    "h45_60m": "45-60m band",
}


def window_entry_timing_from_position(
    *,
    open_ts: float | None = None,
    entry_ts: float | None = None,
    seconds_since_open: float | None = None,
    window_seconds: int | None = None,
    series_label: str = "",
    hourly_entry_bucket: str = "",
) -> dict:
    """Human-readable intra-window entry time for 1h directional trades (dashboard)."""
    tf = market_tf_from_window(series_label=series_label, window_seconds=window_seconds)
    ws = int(window_seconds or 0)
    if tf != "1h" and ws < 3600:
        return {}
    sso = seconds_since_open
    if sso is None and entry_ts is not None and open_ts is not None:
        try:
            sso = max(0.0, float(entry_ts) - float(open_ts))
        except (TypeError, ValueError):
            sso = None
    if sso is None:
        return {}
    sso = max(0.0, min(float(sso), float(ws or 3600)))
    mins = int(sso // 60)
    secs = int(round(sso % 60))
    if secs >= 60:
        mins += 1
        secs = 0
    at = "+%d:%02d" % (mins, secs)
    label = "%s into 1h" % at
    bucket = str(hourly_entry_bucket or "").strip()
    if bucket and bucket not in ("na", "none"):
        band = _HOURLY_BUCKET_LABEL.get(bucket, bucket.replace("_", " "))
        label = "%s (%s)" % (label, band)
    return {
        "window_entry_at": at,
        "window_entry_label": label,
        "window_entry_min": round(sso / 60.0, 1),
        "hourly_entry_bucket": bucket or None,
    }
