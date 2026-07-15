"""Historical / cached Polymarket loader (Gamma API) for backtests.

Uses httpx + on-disk JSON cache under data/cache/. Falls back gracefully
when offline — synthetic generator remains the primary ≥80% WR demo.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from models.market import MarketSnapshot

logger = logging.getLogger(__name__)

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
CACHE_DIR = Path("data/cache")


def _parse_prices(raw: Any) -> tuple[float, float]:
    """Gamma sometimes returns outcomePrices as JSON string."""
    if raw is None:
        return 0.5, 0.5
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return 0.5, 0.5
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            return float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return 0.5, 0.5
    return 0.5, 0.5


def _cache_path(tag: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"gamma_{tag}.json"


def fetch_gamma_markets(
    *,
    limit: int = 100,
    closed: bool = True,
    tag: str = "resolved",
    timeout: float = 20.0,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Pull markets from Gamma; cache to disk."""
    path = _cache_path(tag)
    if use_cache and path.is_file():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass

    params = {"limit": limit, "closed": str(closed).lower(), "active": "false"}
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(GAMMA_MARKETS, params=params)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                data = data.get("markets") or data.get("data") or []
            path.write_text(json.dumps(data[: limit * 2]))
            return list(data)
    except Exception as exc:  # noqa: BLE001 — offline-friendly
        logger.warning("Gamma fetch failed (%s); using cache/empty", exc)
        if path.is_file():
            return json.loads(path.read_text())
        return []


def gamma_to_snapshots(
    rows: list[dict[str, Any]],
    *,
    category: str = "crypto",
) -> list[MarketSnapshot]:
    """Map Gamma rows → MarketSnapshot. q defaults to resolved outcome (oracle).

    For resolved markets we set q near the outcome with small noise so the
    historical path exercises the pipeline; live paper uses CEX/model q.
    """
    out: list[MarketSnapshot] = []
    for i, row in enumerate(rows):
        yes_p, no_p = _parse_prices(row.get("outcomePrices"))
        # Prefer last traded mid if present
        try:
            mid = float(row.get("lastTradePrice") or yes_p)
        except (TypeError, ValueError):
            mid = yes_p

        resolved = row.get("umaResolutionStatus") or row.get("resolved")
        # Best-effort: if closed and price near 0/1, infer outcome
        resolved_yes: Optional[bool] = None
        if mid >= 0.95:
            resolved_yes = True
        elif mid <= 0.05:
            resolved_yes = False

        # Model q: if we know outcome, use slightly shrunk true label;
        # else fall back to mid (no edge → filter rejects).
        if resolved_yes is True:
            q = 0.88
        elif resolved_yes is False:
            q = 0.12
        else:
            q = float(mid)

        slug = str(row.get("slug") or row.get("conditionId") or f"hist_{i}")
        out.append(
            MarketSnapshot(
                market_id=str(row.get("id") or row.get("conditionId") or slug),
                slug=slug,
                question=str(row.get("question") or "")[:200],
                category=category,
                timeframe="1h",
                p=float(min(0.98, max(0.02, mid if 0.05 < mid < 0.95 else yes_p))),
                q=q,
                liquidity_usd=float(row.get("liquidityNum") or row.get("liquidity") or 1000.0),
                volume_24h=float(row.get("volume24hr") or row.get("volume") or 0.0),
                seconds_to_resolution=0.0,
                true_q=1.0 if resolved_yes else (0.0 if resolved_yes is False else None),
                resolved_yes=resolved_yes,
                meta={"source": "gamma", "resolved_flag": resolved},
                as_of=datetime.now(timezone.utc),
            )
        )
    return out


def load_historical(
    *,
    limit: int = 200,
    use_cache: bool = True,
) -> list[MarketSnapshot]:
    rows = fetch_gamma_markets(limit=limit, closed=True, use_cache=use_cache)
    return [m for m in gamma_to_snapshots(rows) if m.resolved_yes is not None]
