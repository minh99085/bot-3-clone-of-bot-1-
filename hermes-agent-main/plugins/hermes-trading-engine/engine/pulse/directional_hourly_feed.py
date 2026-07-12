"""Directional lane feed for Polymarket 1-hour crypto up/down markets (PAPER ONLY).

The hourly directional lane trades BTC + ETH up/down each hour (dated events, e.g.
``bitcoin-up-or-down-july-7-2026-12am-et``).

Auto-discovery (default) picks the currently-open up/down window per asset from the
``btc-up-or-down-hourly`` / ``eth-up-or-down-hourly`` series. Explicit slugs via
``PULSE_DIRECTIONAL_EVENT_SLUGS`` override auto-discovery.

Yes/No outcomes map to up/down tokens internally (Yes = up, No = down). Above markets carry
``strike_price`` for digital fair-value anchoring and settlement proxy.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace
from typing import Callable, Optional

from engine.pulse.markets import (GAMMA, OrderBook, PulseMarketFeed, PulseWindow, _iso_to_unix,
                                  market_fees_enabled)

logger = logging.getLogger("hte.pulse.directional_hourly")

HOURLY_SECONDS = 3600
_UP_DOWN_SERIES = {
    "btc": "btc-up-or-down-hourly",
    "eth": "eth-up-or-down-hourly",
}
_ABOVE_PREFIX = {
    "btc": "bitcoin-above-on",
    "eth": "ethereum-above-on",
}
_STRIKE_SLUG_RE = re.compile(r"above-([\d,]+)-on", re.I)
_STRIKE_Q_RE = re.compile(r"above\s+([\d,]+(?:\.\d+)?)", re.I)


def _parse_strike(slug: str, question: str = "") -> Optional[float]:
    for src in (slug, question):
        m = _STRIKE_SLUG_RE.search(str(src or ""))
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        m = _STRIKE_Q_RE.search(str(src or ""))
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def _parse_outcome_tokens(outcomes, toks) -> tuple[Optional[str], Optional[str], str]:
    """Return (up_token, down_token, market_kind). Up = Up or Yes; down = Down or No."""
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes or "[]")
    if isinstance(toks, str):
        toks = json.loads(toks or "[]")
    if not toks or len(toks) < 2 or not outcomes:
        return None, None, "updown"
    up_tok = down_tok = None
    kind = "updown"
    for name, tok in zip(outcomes, toks):
        n = str(name).strip().lower()
        if n == "up":
            up_tok = str(tok)
        elif n == "down":
            down_tok = str(tok)
        elif n == "yes":
            up_tok = str(tok)
            kind = "above"
        elif n == "no":
            down_tok = str(tok)
            kind = "above"
    if up_tok is None or down_tok is None:
        up_tok, down_tok = str(toks[0]), str(toks[1])
    return up_tok, down_tok, kind


def parse_hourly_market_record(
    m: dict,
    *,
    event_id: str = "",
    event_title: str = "",
    series_slug: str = "directional_hourly",
    series_label: str = "dir_1h",
    window_seconds: int = HOURLY_SECONDS,
) -> Optional[PulseWindow]:
    """Build a :class:`PulseWindow` from a Gamma market dict (single-market fetch)."""
    try:
        toks = m.get("clobTokenIds")
        outs = m.get("outcomes")
        up_tok, down_tok, kind = _parse_outcome_tokens(outs, toks)
        if up_tok is None or down_tok is None:
            return None
        close_ts = _iso_to_unix(m.get("endDate"))
        if close_ts is None:
            return None
        open_ts = _iso_to_unix(m.get("startDate"))
        if open_ts is None or (close_ts - open_ts) > window_seconds * 3:
            open_ts = close_ts - window_seconds
        slug = str(m.get("slug") or "")
        strike = _parse_strike(slug, str(m.get("question") or event_title))
        tick = float(m.get("orderPriceMinTickSize") or 0.01)
        label = series_label
        if kind == "above":
            label = "%s_above" % series_label.split("_")[0]
        return PulseWindow(
            event_id=str(event_id or m.get("id") or slug),
            market_id=str(m.get("id") or ""),
            slug=slug,
            title=str(m.get("question") or event_title or slug),
            open_ts=float(open_ts),
            close_ts=float(close_ts),
            up_token_id=up_tok,
            down_token_id=down_tok,
            tick_size=tick,
            series_slug=series_slug,
            window_seconds=window_seconds,
            series_label=label,
            market_kind=kind,
            strike_price=strike,
            directional_lane=True,
            fees_enabled=market_fees_enabled(m.get("feesEnabled")),
            taker_fee_rate=(0.07 if market_fees_enabled(m.get("feesEnabled")) else 0.0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("parse_hourly_market_record failed: %s", exc)
        return None


def parse_hourly_event(ev: dict, *, series_slug: str = "directional_hourly",
                       series_label: str = "dir_1h") -> Optional[PulseWindow]:
    """Parse a dated up/down hourly Gamma *event* (first market)."""
    markets = ev.get("markets") or []
    if not markets:
        return None
    w = parse_hourly_market_record(
        markets[0],
        event_id=str(ev.get("id") or ""),
        event_title=str(ev.get("title") or ""),
        series_slug=series_slug,
        series_label=series_label,
    )
    if w is None:
        return None
    slug = str(ev.get("slug") or w.slug)
    asset = "eth" if slug.startswith("ethereum") or slug.startswith("eth") else "btc"
    return replace(w, slug=slug, series_label="%s_1h" % asset, market_kind="updown",
                   strike_price=None, directional_lane=True)


def _close_iso_z(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


class DirectionalHourlyMarketFeed:
    """Read-only feed for the four directional 1h markets (explicit slugs or auto-discover)."""

    def __init__(
        self,
        *,
        explicit_slugs: tuple = (),
        auto_discover: bool = True,
        timeout_s: float = 8.0,
        http_get: Optional[Callable] = None,
    ):
        self.explicit_slugs = tuple(s for s in (explicit_slugs or ()) if str(s).strip())
        self.auto_discover = bool(auto_discover) and not self.explicit_slugs
        self.timeout_s = float(timeout_s)
        self._get = http_get
        self._client = None
        self._base = PulseMarketFeed(http_get=http_get, timeout_s=timeout_s)

    def _http(self, url: str, params: dict) -> tuple:
        if self._get is not None:
            return self._get(url, params)
        if self._client is None:
            import httpx
            self._client = httpx.Client(timeout=self.timeout_s,
                                        headers={"User-Agent": "hermes-btc-pulse/1.0"})
        try:
            r = self._client.get(url, params=params)
            return r.status_code, (r.json() if r.status_code == 200 else None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("directional hourly http error %s", exc)
            return 0, None

    def fetch_by_slug(self, slug: str) -> Optional[PulseWindow]:
        slug = str(slug or "").strip()
        if not slug:
            return None
        status, data = self._http(f"{GAMMA}/events", {"slug": slug})
        if status == 200 and isinstance(data, list) and data:
            w = parse_hourly_event(data[0])
            if w is not None:
                return w
        status, data = self._http(f"{GAMMA}/markets", {"slug": slug})
        if status == 200 and isinstance(data, list) and data:
            asset = "eth" if "ethereum" in slug or slug.startswith("eth") else "btc"
            return parse_hourly_market_record(
                data[0], series_slug="directional_hourly", series_label="%s_1h" % asset)
        return None

    def _open_updown_for_asset(self, asset: str, now: float) -> Optional[PulseWindow]:
        series = _UP_DOWN_SERIES.get(asset)
        if not series:
            return None
        status, data = self._http(
            f"{GAMMA}/events",
            {"series_slug": series, "closed": "false", "order": "endDate",
             "ascending": "true", "limit": 40},
        )
        if status != 200 or not isinstance(data, list):
            return None
        label = "%s_1h" % asset
        candidates = []
        for ev in data:
            w = parse_hourly_event(ev, series_slug=series, series_label=label)
            if w is None:
                continue
            if w.open_ts <= now < w.close_ts:
                return w
            if w.close_ts > now:
                candidates.append(w)
        return candidates[0] if candidates else None

    def _atm_above_for_asset(self, asset: str, close_ts: float,
                             spot: Optional[float]) -> Optional[PulseWindow]:
        if spot is None or spot <= 0:
            return None
        end_lo = _close_iso_z(close_ts - 1.0)
        end_hi = _close_iso_z(close_ts + 1.0)
        status, data = self._http(
            f"{GAMMA}/events",
            {"end_date_min": end_lo, "end_date_max": end_hi,
             "closed": "false", "limit": 50},
        )
        if status != 200 or not isinstance(data, list):
            return None
        prefix = _ABOVE_PREFIX.get(asset, "")
        parent = None
        for ev in data:
            slug = str(ev.get("slug") or "")
            if slug.startswith(prefix):
                parent = ev
                break
        if parent is None:
            return None
        best_w = None
        best_dist = None
        for m in parent.get("markets") or []:
            if m.get("closed") or not m.get("enableOrderBook"):
                continue
            try:
                liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            except (TypeError, ValueError):
                liq = 0.0
            if liq < 1.0:
                continue
            strike = _parse_strike(str(m.get("slug") or ""),
                                   str(m.get("question") or ""))
            if strike is None:
                continue
            dist = abs(strike - float(spot))
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_w = parse_hourly_market_record(
                    m,
                    event_id=str(parent.get("id") or ""),
                    event_title=str(parent.get("title") or ""),
                    series_slug="directional_hourly",
                    series_label="%s_above" % asset,
                )
        return best_w

    def discover_windows(self, *, now: Optional[float] = None,
                         btc_spot: Optional[float] = None,
                         eth_spot: Optional[float] = None) -> list:
        """Auto-discover BTC + ETH hourly up/down windows (above strike is a separate lane)."""
        n = float(now if now is not None else time.time())
        out: list = []
        seen: set = set()
        for asset, _spot in (("btc", btc_spot), ("eth", eth_spot)):
            ud = self._open_updown_for_asset(asset, n)
            if ud is not None and ud.slug not in seen:
                out.append(ud)
                seen.add(ud.slug)
        out.sort(key=lambda w: w.close_ts)
        return out

    def active_windows(self, *, now: Optional[float] = None,
                       btc_spot: Optional[float] = None,
                       eth_spot: Optional[float] = None) -> list:
        n = float(now if now is not None else time.time())
        if self.explicit_slugs:
            out = []
            for slug in self.explicit_slugs:
                w = self.fetch_by_slug(slug)
                if w is None:
                    continue
                if w.close_ts <= n:
                    continue
                if w.open_ts <= n + HOURLY_SECONDS:
                    out.append(w)
            out.sort(key=lambda w: w.close_ts)
            return out
        if self.auto_discover:
            return self.discover_windows(now=n, btc_spot=btc_spot, eth_spot=eth_spot)
        return []

    def hydrate_books(self, window: PulseWindow) -> PulseWindow:
        return self._base.hydrate_books(window)

    def fetch_resolution(self, market_id: str) -> Optional[bool]:
        """True = Up/Yes won, False = Down/No won."""
        status, m = self._http(f"{GAMMA}/markets/{market_id}", {})
        if status != 200 or not isinstance(m, dict):
            return None
        outs = m.get("outcomes")
        prices = m.get("outcomePrices")
        if isinstance(outs, str):
            outs = json.loads(outs or "[]")
        if isinstance(prices, str):
            prices = json.loads(prices or "[]")
        if not outs or not prices:
            return None
        try:
            mapping = {str(o).strip().lower(): float(p) for o, p in zip(outs, prices)}
        except (TypeError, ValueError):
            return None
        for up_name in ("up", "yes"):
            for dn_name in ("down", "no"):
                up = mapping.get(up_name)
                dn = mapping.get(dn_name)
                if up is None or dn is None:
                    continue
                if up >= 0.99 and dn <= 0.01:
                    return True
                if dn >= 0.99 and up <= 0.01:
                    return False
        return None

    def owns(self, window: PulseWindow) -> bool:
        if not getattr(window, "directional_lane", False):
            return False
        if getattr(window, "market_kind", "") == "above":
            return False
        slug = str(getattr(window, "series_slug", "") or "")
        if slug in _UP_DOWN_SERIES.values() or slug == "directional_hourly":
            return True
        label = str(getattr(window, "series_label", "") or "")
        if label.endswith("_above"):
            return False
        return label.endswith("_1h")

    def report(self) -> dict:
        return {
            "enabled": True,
            "mode": ("explicit_slugs" if self.explicit_slugs
                     else ("auto_discover" if self.auto_discover else "off")),
            "explicit_slugs": list(self.explicit_slugs),
            "market_kinds": ["updown"],
        }
