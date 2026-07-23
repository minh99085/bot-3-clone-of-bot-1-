"""Market scope + MARKET_FILTER for multi-instance Hermes.

Instances (docker-compose):
  hermes-btc5   → MARKET_FILTER=btc5
  hermes-btc15  → MARKET_FILTER=btc15
  hermes-eth5   → MARKET_FILTER=eth5
  hermes-sol5   → MARKET_FILTER=sol5
  hermes-rotator→ MARKET_FILTER=rotator  (all four; top conviction only)

Win-rate filters (min_edge / conviction / Kelly) live in enhanced_misprice.yaml
and are NOT changed here — this module only selects *which markets* are eligible.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

# Canonical series IDs (substrategy / lessons / dashboard)
SERIES_BTC_5M = "btc_updown_5m"
SERIES_BTC_15M = "btc_updown_15m"
SERIES_ETH_5M = "eth_updown_5m"
SERIES_ETH_15M = "eth_updown_15m"
SERIES_SOL_5M = "sol_updown_5m"
SERIES_5M = SERIES_BTC_5M  # backward-compat alias
SERIES_15M = SERIES_BTC_15M  # backward-compat alias

ALL_SERIES = frozenset(
    {SERIES_BTC_5M, SERIES_BTC_15M, SERIES_ETH_5M, SERIES_ETH_15M, SERIES_SOL_5M}
)
ALLOWED_SERIES = ALL_SERIES  # expanded universe; filter narrows per instance

SLUG_RE = re.compile(r"^(btc|eth|sol)-updown-(5m|15m)-(\d+)$")

FILTER_KEYS = frozenset({"btc5", "btc15", "eth5", "eth15", "sol5", "rotator"})

# Fast-market sizing defaults (paper, $2000 bankroll) — unchanged
COLD_START_SIZE_PCT = 0.005  # 0.5% of bankroll (~$10)
MAX_SIZE_PCT_FAST = 0.02  # 2% cap until lessons prove edge
MIN_SIZE_PCT_FAST = 0.005
MIN_LIVE_EV_FAST = 0.04
MIN_ORACLE_ALIGN = 0.55

# Reject entries too close to window end or on already-expired slugs.
MIN_WINDOW_REMAINING_SEC = int(os.environ.get("HERMES_MIN_WINDOW_REMAINING_SEC", "60"))
WINDOW_SETTLE_GRACE_SEC = int(os.environ.get("HERMES_WINDOW_SETTLE_GRACE_SEC", "15"))

# Block penny / resolved-side paper fills (matches verifier + discovery).
EXTREME_PRICE_LOW = float(os.environ.get("HERMES_EXTREME_PRICE_LOW", "0.02"))
EXTREME_PRICE_HIGH = float(os.environ.get("HERMES_EXTREME_PRICE_HIGH", "0.98"))
# Verifier/signal: reject lottery tickets on either contract side.
MIN_TRADABLE_ENTRY_LOW = float(os.environ.get("HERMES_MIN_TRADABLE_ENTRY_LOW", "0.25"))
MIN_TRADABLE_ENTRY_HIGH = float(os.environ.get("HERMES_MIN_TRADABLE_ENTRY_HIGH", "0.90"))

# Legacy preferred seeds (also listed in market_filters.yaml)
PREFERRED_SLUGS = (
    "btc-updown-15m-1784113200",
    "btc-updown-5m-1784113500",
    "eth-updown-5m-1784113500",
    "sol-updown-5m-1784113500",
)

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "market_filters.yaml"


@dataclass(frozen=True)
class ScopedMarket:
    series: str
    timeframe: str  # 5m | 15m
    slug: str
    window_ts: int
    asset: str = "btc"  # btc | eth | sol
    filter_key: str = ""  # btc5 | btc15 | eth5 | sol5


@dataclass(frozen=True)
class MarketFilterSpec:
    key: str
    label: str
    asset: str
    timeframe: str
    series: str
    slug_prefix: str
    cex_symbol: str = "BTCUSDT"
    oracle_asset: str = "BTC"


@lru_cache(maxsize=1)
def load_market_filters_config() -> dict[str, Any]:
    if _CONFIG_PATH.is_file():
        with _CONFIG_PATH.open() as f:
            return yaml.safe_load(f) or {}
    return {}


def filter_specs() -> dict[str, MarketFilterSpec]:
    raw = load_market_filters_config().get("filters") or {}
    out: dict[str, MarketFilterSpec] = {}
    for key, row in raw.items():
        if not isinstance(row, dict):
            continue
        out[key] = MarketFilterSpec(
            key=key,
            label=str(row.get("label") or key),
            asset=str(row.get("asset") or "btc").lower(),
            timeframe=str(row.get("timeframe") or "5m").lower(),
            series=str(row.get("series") or ""),
            slug_prefix=str(row.get("slug_prefix") or ""),
            cex_symbol=str(row.get("cex_symbol") or "BTCUSDT"),
            oracle_asset=str(row.get("oracle_asset") or "BTC"),
        )
    # Hard-coded fallbacks if YAML missing
    if not out:
        out = {
            "btc5": MarketFilterSpec(
                "btc5", "BTC 5m", "btc", "5m", SERIES_BTC_5M, "btc-updown-5m-"
            ),
            "btc15": MarketFilterSpec(
                "btc15", "BTC 15m", "btc", "15m", SERIES_BTC_15M, "btc-updown-15m-"
            ),
            "eth5": MarketFilterSpec(
                "eth5", "ETH 5m", "eth", "5m", SERIES_ETH_5M, "eth-updown-5m-",
                "ETHUSDT", "ETH",
            ),
            "eth15": MarketFilterSpec(
                "eth15", "ETH 15m", "eth", "15m", SERIES_ETH_15M, "eth-updown-15m-",
                "ETHUSDT", "ETH",
            ),
            "sol5": MarketFilterSpec(
                "sol5", "SOL 5m", "sol", "5m", SERIES_SOL_5M, "sol-updown-5m-",
                "SOLUSDT", "SOL",
            ),
        }
    return out


def market_filter() -> str:
    """Active MARKET_FILTER from env (default btc5+btc15 legacy → rotator-safe 'legacy').

    Values: btc5 | btc15 | eth5 | sol5 | rotator | all
    Empty / unset with HERMES_SCOPE_BTC_UPDOWN_ONLY=1 → both BTC lanes (legacy).
    """
    raw = os.environ.get("MARKET_FILTER", "").strip().lower()
    if raw in FILTER_KEYS or raw == "all":
        return raw
    # Legacy: no MARKET_FILTER → BTC 5m+15m only (previous single-bot behavior)
    if scope_enabled():
        return "legacy_btc"
    return "all"


def is_rotator() -> bool:
    return market_filter() == "rotator"


def scope_enabled() -> bool:
    """Keep legacy env gate; multi-instance always scopes via MARKET_FILTER."""
    mf = os.environ.get("MARKET_FILTER", "").strip().lower()
    if mf in FILTER_KEYS or mf == "all":
        return True
    return os.environ.get("HERMES_SCOPE_BTC_UPDOWN_ONLY", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def preferred_slugs() -> tuple[str, ...]:
    raw = os.environ.get("HERMES_BTC_UPDOWN_SLUGS", "").strip()
    if raw:
        return tuple(s.strip() for s in raw.split(",") if s.strip())
    cfg = load_market_filters_config()
    prefs = cfg.get("preferred_slugs")
    if isinstance(prefs, list) and prefs:
        return tuple(str(s) for s in prefs)
    return PREFERRED_SLUGS


def filter_key_for_scoped(sm: ScopedMarket) -> str:
    if sm.asset == "btc" and sm.timeframe == "5m":
        return "btc5"
    if sm.asset == "btc" and sm.timeframe == "15m":
        return "btc15"
    if sm.asset == "eth" and sm.timeframe == "5m":
        return "eth5"
    if sm.asset == "sol" and sm.timeframe == "5m":
        return "sol5"
    return ""


def parse_slug(slug: str) -> Optional[ScopedMarket]:
    m = SLUG_RE.match((slug or "").strip().lower())
    if not m:
        return None
    asset, tf, ts_s = m.group(1), m.group(2), m.group(3)
    ts = int(ts_s)
    series = f"{asset}_updown_{tf}"
    sm = ScopedMarket(
        series=series,
        timeframe=tf,
        slug=slug.strip().lower(),
        window_ts=ts,
        asset=asset,
        filter_key="",
    )
    return ScopedMarket(
        series=sm.series,
        timeframe=sm.timeframe,
        slug=sm.slug,
        window_ts=sm.window_ts,
        asset=sm.asset,
        filter_key=filter_key_for_scoped(sm),
    )


def is_allowed_slug(slug: str, *, market_filter: Optional[str] = None) -> bool:
    """True if slug matches the active (or provided) market filter."""
    sm = parse_slug(slug)
    if sm is None:
        return False
    return slug_matches_filter(sm, market_filter=market_filter)


def slug_matches_filter(
    sm: ScopedMarket, *, market_filter: Optional[str] = None
) -> bool:
    mf = (market_filter or market_filter_from_env()).strip().lower()
    if mf in ("", "all", "rotator"):
        return sm.filter_key in ("btc5", "btc15", "eth5", "sol5")
    if mf == "legacy_btc":
        return sm.asset == "btc" and sm.timeframe in ("5m", "15m")
    return sm.filter_key == mf


def market_filter_from_env() -> str:
    return market_filter()


def is_allowed_series(series: str, *, market_filter: Optional[str] = None) -> bool:
    mf = (market_filter or market_filter_from_env()).strip().lower()
    if series not in ALL_SERIES:
        return False
    if mf in ("", "all", "rotator"):
        return True
    if mf == "legacy_btc":
        return series in (SERIES_BTC_5M, SERIES_BTC_15M)
    specs = filter_specs()
    spec = specs.get(mf)
    return bool(spec and series == spec.series)


def active_filter_keys(*, market_filter: Optional[str] = None) -> list[str]:
    mf = (market_filter or market_filter_from_env()).strip().lower()
    if mf in ("btc5", "btc15", "eth5", "eth15", "sol5"):
        return [mf]
    if mf == "legacy_btc":
        return ["btc5", "btc15"]
    # rotator / all
    return ["btc5", "btc15", "eth5", "eth15", "sol5"]


def series_from_record(record: dict) -> Optional[str]:
    """Resolve series without substring false positives (5m vs 15m)."""
    meta = record.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    ms = str(record.get("market_series") or meta.get("market_series") or "").strip()
    if ms in ALL_SERIES:
        return ms

    slug = str(
        record.get("slug")
        or record.get("market_slug")
        or meta.get("slug")
        or ""
    ).strip().lower()
    if slug:
        sm = parse_slug(slug)
        if sm:
            return sm.series

    sid = str(record.get("substrategy_id") or meta.get("substrategy_id") or "").strip()
    if sid:
        head = sid.split("|", 1)[0].strip()
        if head in ALL_SERIES:
            return head

    asset = str(
        record.get("asset") or meta.get("asset") or ""
    ).strip().lower()
    tf = str(record.get("timeframe") or meta.get("timeframe") or "").strip().lower()
    if asset in ("btc", "eth", "sol") and tf in ("5m", "15m"):
        return f"{asset}_updown_{tf}"
    if tf == "5m" and not asset:
        return SERIES_BTC_5M
    if tf == "15m" and not asset:
        return SERIES_BTC_15M

    return None


def record_belongs_to_series(record: dict, series: str) -> bool:
    resolved = series_from_record(record)
    return resolved == series if resolved else False


def series_from_slug(slug: str) -> Optional[str]:
    sm = parse_slug(slug)
    return sm.series if sm else None


def window_step_seconds(timeframe: str) -> int:
    return 300 if timeframe == "5m" else 900


def window_end_ts_for_slug(slug: str) -> Optional[int]:
    """Unix timestamp when the up/down window closes."""
    sm = parse_slug(slug)
    if not sm:
        return None
    return sm.window_ts + window_step_seconds(sm.timeframe)


def window_remaining_seconds(slug: str, *, now: Optional[float] = None) -> Optional[float]:
    """Seconds until window end; negative if already expired."""
    end = window_end_ts_for_slug(slug)
    if end is None:
        return None
    t = float(now if now is not None else time.time())
    return float(end) - t


def is_window_tradeable(slug: str, *, now: Optional[float] = None) -> bool:
    """True when the window is open and enough time remains to enter."""
    sm = parse_slug(slug)
    if sm is None:
        return True  # non up/down slugs: other gates handle scope
    rem = window_remaining_seconds(slug, now=now)
    if rem is None:
        return False
    return rem >= MIN_WINDOW_REMAINING_SEC


def is_window_expired(
    slug: str, *, now: Optional[float] = None, grace_sec: int = WINDOW_SETTLE_GRACE_SEC
) -> bool:
    """True when window end + grace has passed (ok to settle)."""
    rem = window_remaining_seconds(slug, now=now)
    if rem is None:
        return True
    return rem <= -grace_sec


def is_extreme_market_price(yes_price: float) -> bool:
    """True when YES is at lottery/resolution tail — not tradable for paper desk."""
    p = float(yes_price)
    return p <= EXTREME_PRICE_LOW or p >= EXTREME_PRICE_HIGH


def entry_price_for_side(yes_price: float, direction: str) -> float:
    """Price paid for the chosen contract side."""
    d = (direction or "").upper()
    if d in ("UP", "YES"):
        return float(yes_price)
    return 1.0 - float(yes_price)


def is_extreme_entry_price(yes_price: float, direction: str) -> bool:
    px = entry_price_for_side(yes_price, direction)
    return px <= MIN_TRADABLE_ENTRY_LOW or px >= MIN_TRADABLE_ENTRY_HIGH


def resolve_asset(
    slug: str = "",
    *,
    meta: Optional[dict] = None,
    default: str = "BTC",
) -> str:
    """Resolve BTC/ETH/SOL from slug, then meta — never guess wrong asset for SOL."""
    sm = parse_slug(slug) if slug else None
    if sm:
        return sm.asset.upper()
    meta = meta or {}
    slug = slug or str(meta.get("slug") or "")
    if slug:
        sm = parse_slug(slug)
        if sm:
            return sm.asset.upper()
    for key in ("cex_asset", "asset"):
        val = meta.get(key)
        if val:
            au = str(val).upper()
            if au in ("BTC", "ETH", "SOL"):
                return au
    series = str(meta.get("scoped_series") or meta.get("market_series") or "")
    for prefix, asset in (("btc_", "BTC"), ("eth_", "ETH"), ("sol_", "SOL")):
        if series.startswith(prefix):
            return asset
    blob = f"{slug} {meta.get('question', '')}".lower()
    if "sol" in blob or "solana" in blob:
        return "SOL"
    if "eth" in blob or "ethereum" in blob:
        return "ETH"
    if "btc" in blob or "bitcoin" in blob:
        return "BTC"
    return default.upper()


def current_window_ts(timeframe: str, *, now: Optional[float] = None) -> int:
    step = window_step_seconds(timeframe)
    t = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    return (t // step) * step


def candidate_slugs_for_filter(
    filter_key: str, *, now: Optional[float] = None
) -> list[str]:
    specs = filter_specs()
    spec = specs.get(filter_key)
    if not spec:
        return []
    step = window_step_seconds(spec.timeframe)
    base = current_window_ts(spec.timeframe, now=now)
    out: list[str] = []
    for pref in preferred_slugs():
        sm = parse_slug(pref)
        if sm and sm.filter_key == filter_key:
            out.append(sm.slug)
    # Current + upcoming windows only (no -1: that slug is usually already expired).
    for off in (0, 1, 2, 3):
        slug = f"{spec.slug_prefix}{base + off * step}"
        if slug not in out and is_window_tradeable(slug, now=now):
            out.append(slug)
    # Drop stale preferred seeds
    return [s for s in out if is_window_tradeable(s, now=now)]


def candidate_slugs_for_series(timeframe: str, *, now: Optional[float] = None) -> list[str]:
    """Backward-compat: BTC-only series helper used by older callers."""
    key = "btc5" if timeframe == "5m" else "btc15"
    return candidate_slugs_for_filter(key, now=now)


def all_discovery_slugs(
    *, now: Optional[float] = None, market_filter: Optional[str] = None
) -> list[str]:
    """Ordered slug candidates for the active MARKET_FILTER."""
    seen: set[str] = set()
    ordered: list[str] = []
    for key in active_filter_keys(market_filter=market_filter):
        for slug in candidate_slugs_for_filter(key, now=now):
            if slug not in seen:
                seen.add(slug)
                ordered.append(slug)
    return ordered


def matches_market_filter(
    *,
    slug: str = "",
    series: str = "",
    asset: str = "",
    timeframe: str = "",
    market_filter: Optional[str] = None,
) -> bool:
    """Strategy-facing helper: does this market belong to the active filter?"""
    if slug:
        return is_allowed_slug(slug, market_filter=market_filter)
    if series:
        return is_allowed_series(series, market_filter=market_filter)
    asset_l = (asset or "").lower()
    tf = (timeframe or "").lower()
    if asset_l and tf:
        synthetic = f"{asset_l}-updown-{tf}-0"
        # parse needs digits; build synthetic ScopedMarket
        series_id = f"{asset_l}_updown_{tf}"
        sm = ScopedMarket(
            series=series_id,
            timeframe=tf,
            slug=synthetic,
            window_ts=0,
            asset=asset_l,
            filter_key=filter_key_for_scoped(
                ScopedMarket(series_id, tf, synthetic, 0, asset_l, "")
            ),
        )
        return slug_matches_filter(sm, market_filter=market_filter)
    return False


def instance_id() -> str:
    """Stable instance id for logs/data isolation (btc5, eth5, rotator, …)."""
    env = os.environ.get("HERMES_INSTANCE_ID", "").strip().lower()
    if env:
        return env
    mf = market_filter()
    if mf == "legacy_btc":
        return "legacy"
    if mf == "all":
        return "default"
    return mf or "default"
