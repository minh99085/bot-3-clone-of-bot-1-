"""Paper settlement for scoped BTC/ETH/SOL 5m/15m Up/Down windows.

Resolves open paper positions when the market window has elapsed, using
per-asset CEX mid change (Binance) as the direction oracle.

Feeds lessons + bandit rewards so Option D can learn online.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from connectors.cex_realtime import get_asset_mid
from hermes.bandit import get_bandit
from hermes.lessons_engine import process_settlement
from hermes.market_scope import (
    is_window_expired,
    parse_slug,
    resolve_asset,
    window_step_seconds,
)
from hermes.models import (
    ConfidenceTier,
    Direction,
    EntryMode,
    Regime,
    Settlement,
)
from hermes.state_io import DATA, append_jsonl, ledger_path, read_jsonl

logger = logging.getLogger(__name__)

# Cap lottery PnL from penny entries in paper mode.
MIN_ENTRY_PX_FOR_PNL = float(os.environ.get("HERMES_MIN_ENTRY_PX_FOR_PNL", "0.02"))
MAX_WIN_PNL_MULTIPLE = float(os.environ.get("HERMES_MAX_WIN_PNL_MULTIPLE", "5.0"))

# Fleet-shared open/close refs so every lane settles the same window identically.
_SHARED_REFS_PATH = DATA / "paper" / "_shared" / "settlement_refs.json"


def _open_positions(paper: bool = True) -> list[dict]:
    rows = read_jsonl(ledger_path(paper=paper))
    opens = [r for r in rows if r.get("event") == "position_open"]
    settled = {
        r.get("signal_id") or r.get("position_id")
        for r in rows
        if r.get("event") == "settlement"
    }
    out = []
    for o in opens:
        sid = o.get("signal_id")
        if sid and sid in settled:
            continue
        out.append(o)
    return out


def _resolve_asset(slug: str, meta: dict) -> str:
    return resolve_asset(slug, meta=meta)


def _cap_win_pnl(pnl: float, size: float) -> float:
    cap = size * MAX_WIN_PNL_MULTIPLE
    return min(pnl, cap) if pnl > 0 else pnl


def _cex_plausible(asset: str, px: float) -> bool:
    if px <= 0:
        return False
    bands = {"BTC": (1_000.0, 500_000.0), "ETH": (100.0, 50_000.0), "SOL": (1.0, 5_000.0)}
    lo, hi = bands.get(asset.upper(), (0.0, 1e12))
    return lo <= px <= hi


def _resolution_price_at(asset: str, ts: int) -> float:
    """Price at ``ts`` for reconstructing an up/down outcome from a strike.

    This is the FALLBACK path — the primary settlement source is Polymarket's
    actual resolved outcome (see ``_polymarket_resolution``). A 15-minute window
    does not need a paid Chainlink data stream: use the CEX price at the epoch,
    and only if that is unavailable fall back to the free on-chain AggregatorV3.
    Returns 0.0 if no source yields a plausible price (caller then skips).
    """
    a = (asset or "").upper()
    try:
        from connectors.cex_realtime import price_at_timestamp

        px = float(price_at_timestamp(a, int(ts)) or 0.0)
        if px > 0:
            return px
    except Exception as exc:  # noqa: BLE001
        logger.debug("cex price lookup failed asset=%s ts=%s: %s", a, ts, exc)
    if a in ("BTC", "ETH"):
        try:
            from connectors.chainlink import oracle_agg_price_at

            return float(oracle_agg_price_at(a, int(ts)) or 0.0)  # free, no creds
        except Exception as exc:  # noqa: BLE001
            logger.debug("aggregatorv3 fallback failed asset=%s ts=%s: %s", a, ts, exc)
    return 0.0


def _open_price_at(asset: str, window_ts: int) -> float:
    """Resolution strike = Chainlink stream price at the window-open epoch."""
    return _resolution_price_at(asset, int(window_ts))


def _close_price_at(asset: str, close_ts: int) -> float:
    """Resolution close = Chainlink stream price AT the window close.

    Settlement runs minutes after close; sampling any LIVE price there lets
    post-close drift flip outcomes (proven live when lanes settled the same
    window with different results). Must be the stream value AT close_ts.
    """
    return _resolution_price_at(asset, int(close_ts))


def _polymarket_resolution(slug: str) -> Optional[bool]:
    """Polymarket's actual resolved outcome for ``slug`` (UP=True/DOWN=False),
    or None if not yet resolved / unavailable. This is the settlement ground
    truth — the market's own data, no price feed required."""
    if not slug:
        return None
    try:
        from connectors.polymarket import PolymarketClient

        return PolymarketClient().get_market_resolution(slug)
    except Exception as exc:  # noqa: BLE001
        logger.debug("polymarket resolution lookup failed %s: %s", slug, exc)
        return None


def shared_refs_path() -> Path:
    return Path(os.environ.get("HERMES_SETTLEMENT_REFS", str(_SHARED_REFS_PATH)))


def _load_shared_refs() -> dict:
    path = shared_refs_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("shared settlement refs read failed: %s", exc)
        return {}


def _store_shared_ref(
    slug: str,
    *,
    moved_up: bool,
    source: str,
    open_px: float = 0.0,
    close_px: float = 0.0,
) -> None:
    """Persist a fleet-wide outcome so every lane settles the window the same way."""
    if not slug:
        return
    path = shared_refs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    refs = _load_shared_refs()
    refs[slug] = {
        "moved_up": bool(moved_up),
        "source": source,
        "open_px": float(open_px or 0.0),
        "close_px": float(close_px or 0.0),
        "saved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(refs, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def settlement_pnl_usd(*, won: bool, size_usd: float, entry_price: float) -> float:
    """Canonical paper PnL for a settled binary ticket (matches live settlement)."""
    size = float(size_usd or 0.0)
    if not won:
        return round(-size, 2)
    eff_entry = max(float(entry_price or 0.5), MIN_ENTRY_PX_FOR_PNL)
    return round(_cap_win_pnl(size * (1.0 / eff_entry - 1.0), size), 2)


def resolve_window_moved_up(
    slug: str,
    *,
    asset: str,
    window_ts: Optional[int] = None,
    window_end: Optional[float] = None,
) -> tuple[Optional[bool], str]:
    """Deterministic UP/DOWN resolution shared across the fleet.

    Order:
      1. Polymarket resolved outcome (ground truth)
      2. Fleet-shared cache (first lane to settle wins the ref for everyone)
      3. CEX open @ window_ts vs close @ window_end (never per-lane strike/mid)

    Returns ``(moved_up, note)`` or ``(None, note)`` when unresolved.
    """
    resolved_up = _polymarket_resolution(slug)
    if resolved_up is not None:
        _store_shared_ref(slug, moved_up=resolved_up, source="polymarket")
        return resolved_up, "settle_polymarket_resolution"

    cached = _load_shared_refs().get(slug) or {}
    if "moved_up" in cached:
        src = cached.get("source") or "shared_cache"
        open_px = float(cached.get("open_px") or 0.0)
        close_px = float(cached.get("close_px") or 0.0)
        if open_px > 0 and close_px > 0:
            note = (
                f"settle_shared_cache src={src} "
                f"open_cex={open_px:.4f} exit_cex={close_px:.4f}"
            )
        else:
            note = f"settle_shared_cache src={src}"
        return bool(cached["moved_up"]), note

    # Do NOT use per-lane meta strike / price_to_beat / entry mid — those
    # differ across containers and flip tight windows (proven live: same
    # slug+side settled win in one lane and loss in another).
    open_ref = 0.0
    exit_cex = 0.0
    if window_ts is not None:
        open_ref = _open_price_at(asset, int(window_ts))
    if window_end is not None:
        exit_cex = _close_price_at(asset, int(window_end))
    if not (_cex_plausible(asset, open_ref) and _cex_plausible(asset, exit_cex)):
        return None, (
            f"unresolved open_cex={open_ref:.4f} exit_cex={exit_cex:.4f}"
        )
    moved_up = exit_cex >= open_ref
    note = f"settle_cex_openref open_cex={open_ref:.4f} exit_cex={exit_cex:.4f}"
    _store_shared_ref(
        slug,
        moved_up=moved_up,
        source="cex_openref",
        open_px=open_ref,
        close_px=exit_cex,
    )
    return moved_up, note


def settle_expired_paper_positions(paper: bool = True) -> list[Settlement]:
    """Settle positions whose up/down window has ended (+ grace)."""
    now = time.time()
    out: list[Settlement] = []

    for pos in _open_positions(paper=paper):
        slug = str(pos.get("slug") or "")
        meta = pos.get("meta") or {}
        slug = slug or str(meta.get("slug") or "")
        sm = parse_slug(slug) if slug else None
        asset = _resolve_asset(slug, meta)

        window_end: Optional[float] = None
        if sm:
            if not is_window_expired(slug, now=now):
                continue
            window_end = sm.window_ts + window_step_seconds(sm.timeframe)
        else:
            opened = pos.get("opened_at") or pos.get("created_at") or ""
            try:
                if opened.endswith("Z"):
                    opened = opened.replace("Z", "+00:00")
                ts = datetime.fromisoformat(str(opened)).timestamp()
                window_end = ts + 360
            except Exception:
                window_end = now - 1

        if window_end and now < window_end + 15:
            continue

        direction = pos.get("direction") or "DOWN"
        if isinstance(direction, str):
            try:
                direction = Direction(direction)
            except ValueError:
                direction = Direction.DOWN

        entry_px = float(pos.get("entry_price") or 0.5)
        size = float(pos.get("size_usd") or 0)
        entry_asset = _resolve_asset(slug, meta)

        moved_up, ref_note = resolve_window_moved_up(
            slug,
            asset=entry_asset,
            window_ts=(sm.window_ts if sm else None),
            window_end=window_end,
        )
        if moved_up is None:
            logger.warning("skip settle %s: %s", slug, ref_note)
            continue

        if direction in (Direction.UP, Direction.YES):
            won = moved_up
        else:
            won = not moved_up
        exit_px = 1.0 if won else 0.0
        notes = (
            f"{ref_note} asset={entry_asset} "
            f"bandit_arm={meta.get('bandit_arm')} "
            f"bandit_ctx={meta.get('bandit_context')}"
        )
        pnl = settlement_pnl_usd(won=won, size_usd=size, entry_price=entry_px)

        stl = Settlement(
            position_id=str(pos.get("position_id") or pos.get("signal_id") or ""),
            signal_id=str(pos.get("signal_id") or ""),
            market_id=str(pos.get("market_id") or ""),
            direction=direction if isinstance(direction, Direction) else Direction.DOWN,
            entry_price=entry_px,
            exit_price=exit_px,
            size_usd=size,
            pnl_usd=round(pnl, 2),
            won=won,
            regime=Regime.MEAN_REVERT,
            hourly_bucket=int(datetime.now(timezone.utc).hour),
            entry_mode=EntryMode.MISPRICING
            if meta.get("entry_source") in ("mispricing", "enhanced_mispricing")
            else EntryMode.MEAN_REVERSION,
            confidence_tier=ConfidenceTier.B,
            market_series=str(meta.get("market_series") or (sm.series if sm else "btc_updown_5m")),
            substrategy_id=str(meta.get("substrategy_id") or ""),
            slug=slug,
            timeframe=(sm.timeframe if sm else str(meta.get("timeframe") or "5m")),
            paper=paper,
            notes=notes,
        )
        append_jsonl(
            ledger_path(paper=paper),
            {"event": "settlement", **stl.model_dump(mode="json")},
        )
        process_settlement(stl)
        out.append(stl)
        logger.info(
            "SETTLE %s won=%s pnl=$%.2f :: %s",
            stl.market_id,
            won,
            stl.pnl_usd,
            notes[:100],
        )
    return out
