"""Directional lane feed for Polymarket 15-minute BTC/ETH up/down markets (PAPER ONLY).

Separate from the hourly directional feed. Auto-discovers the currently-open 15m window
per asset from ``btc-up-or-down-15m`` / ``eth-up-or-down-15m``. Windows are marked
``directional_lane=True`` so they enter the tick + tier-engine path (not Osmani fill).

History (2026-07): Polymarket 15m resolves ~50/50 UP/DOWN; bot BTC 15m cohort was +EV
but low WR; ETH 15m had n=1 disaster. Lane starts lightly gated; LaneStrategyLearner
raises selectivity from settled outcomes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Callable, Optional

from engine.pulse.directional_hourly_feed import (
    parse_hourly_event,
    parse_hourly_market_record,
)
from engine.pulse.markets import (
    GAMMA,
    SERIES_SLUG_15M,
    SERIES_SLUG_ETH_15M,
    WINDOW_SECONDS_15M,
    PulseMarketFeed,
    PulseWindow,
)

logger = logging.getLogger("hte.pulse.directional_15m")

WINDOW_SECONDS = WINDOW_SECONDS_15M  # 900
_UP_DOWN_SERIES = {
    "btc": SERIES_SLUG_15M,
    "eth": SERIES_SLUG_ETH_15M,
}


class Directional15mMarketFeed:
    """Read-only feed for BTC + ETH 15m up/down directional windows."""

    def __init__(
        self,
        *,
        auto_discover: bool = True,
        assets: tuple = ("btc", "eth"),
        timeout_s: float = 8.0,
        http_get: Optional[Callable] = None,
    ):
        self.auto_discover = bool(auto_discover)
        self.assets = tuple(a for a in (assets or ()) if a in _UP_DOWN_SERIES)
        self.timeout_s = float(timeout_s)
        self._get = http_get
        self._client = None
        self._base = PulseMarketFeed(http_get=http_get, timeout_s=timeout_s)

    def _http(self, url: str, params: dict) -> tuple:
        if self._get is not None:
            return self._get(url, params)
        if self._client is None:
            import httpx
            self._client = httpx.Client(
                timeout=self.timeout_s,
                headers={"User-Agent": "hermes-btc-pulse/1.0"},
            )
        try:
            r = self._client.get(url, params=params)
            return r.status_code, (r.json() if r.status_code == 200 else None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("directional 15m http error %s", exc)
            return 0, None

    def _open_updown_for_asset(self, asset: str, now: float) -> Optional[PulseWindow]:
        series = _UP_DOWN_SERIES.get(asset)
        if not series:
            return None
        status, data = self._http(
            f"{GAMMA}/events",
            {
                "series_slug": series,
                "closed": "false",
                "order": "endDate",
                "ascending": "true",
                "limit": 40,
            },
        )
        if status != 200 or not isinstance(data, list):
            return None
        label = "%s_15m" % asset
        candidates = []
        for ev in data:
            w = parse_hourly_event(ev, series_slug=series, series_label=label)
            if w is None:
                continue
            # Force 15m window semantics (Gamma startDate can be far earlier than open).
            open_ts = float(w.close_ts) - float(WINDOW_SECONDS)
            w = replace(
                w,
                open_ts=open_ts,
                window_seconds=WINDOW_SECONDS,
                series_slug=series,
                series_label=label,
                market_kind="updown",
                strike_price=None,
                directional_lane=True,
            )
            if w.open_ts <= now < w.close_ts:
                return w
            if w.close_ts > now:
                candidates.append(w)
        return candidates[0] if candidates else None

    def discover_windows(self, *, now: Optional[float] = None) -> list:
        n = float(now if now is not None else time.time())
        out: list = []
        seen: set = set()
        for asset in self.assets:
            ud = self._open_updown_for_asset(asset, n)
            if ud is not None and ud.slug not in seen:
                out.append(ud)
                seen.add(ud.slug)
        out.sort(key=lambda w: w.close_ts)
        return out

    def active_windows(self, *, now: Optional[float] = None, **_kwargs) -> list:
        if not self.auto_discover:
            return []
        return self.discover_windows(now=now)

    def hydrate_books(self, window: PulseWindow) -> PulseWindow:
        return self._base.hydrate_books(window)

    def fetch_resolution(self, market_id: str) -> Optional[bool]:
        status, m = self._http(f"{GAMMA}/markets/{market_id}", {})
        if status != 200 or not isinstance(m, dict):
            return None
        import json
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
        slug = str(getattr(window, "series_slug", "") or "")
        if slug in _UP_DOWN_SERIES.values():
            return True
        label = str(getattr(window, "series_label", "") or "")
        return label.endswith("_15m")

    def report(self) -> dict:
        return {
            "enabled": True,
            "mode": "auto_discover" if self.auto_discover else "off",
            "assets": list(self.assets),
            "series": [_UP_DOWN_SERIES[a] for a in self.assets],
            "window_seconds": WINDOW_SECONDS,
            "market_kinds": ["updown"],
        }
