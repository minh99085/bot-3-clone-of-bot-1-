#!/usr/bin/env python3
"""Download Polymarket BTC/ETH 15m + 1h closed windows for bot training.

Fetches Gamma event metadata, CLOB price history, and Data API trades.
Writes resumable checkpoints under --output (default: data/polymarket-training).

Usage (from repo root):
  python3 scripts/polymarket-backfill/download_crypto_windows.py
  python3 scripts/polymarket-backfill/download_crypto_windows.py --days 30 --with-trades
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

SCRIPT = Path(__file__).resolve()


def _resolve_engine_root() -> Path:
    """Repo checkout or /app inside hermes-training/backfill containers."""
    repo_root = SCRIPT.parents[2]
    candidates = (
        repo_root / "hermes-agent-main" / "plugins" / "hermes-trading-engine",
        Path("/app"),
        Path("/backfill_engine"),
    )
    for candidate in candidates:
        if (candidate / "engine" / "pulse" / "markets.py").is_file():
            return candidate
    return candidates[0]


ENGINE = _resolve_engine_root()
ROOT = ENGINE.parents[2] if ENGINE.name == "hermes-trading-engine" else ENGINE
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from engine.pulse.directional_hourly_feed import parse_hourly_event  # noqa: E402
from engine.pulse.markets import GAMMA, WINDOW_SECONDS_15M  # noqa: E402

GAMMA_API = GAMMA
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

SERIES = (
    {"asset": "btc", "lane": "15m", "series_slug": "btc-up-or-down-15m", "window_seconds": 900},
    {"asset": "eth", "lane": "15m", "series_slug": "eth-up-or-down-15m", "window_seconds": 900},
    {"asset": "btc", "lane": "1h", "series_slug": "btc-up-or-down-hourly", "window_seconds": 3600},
    {"asset": "eth", "lane": "1h", "series_slug": "eth-up-or-down-hourly", "window_seconds": 3600},
)

log = logging.getLogger("polymarket_backfill")


@dataclass
class WindowRecord:
    event_id: str
    event_slug: str
    market_id: str
    condition_id: str
    asset: str
    lane: str
    series_slug: str
    window_seconds: int
    open_ts: float
    close_ts: float
    title: str
    up_token_id: str
    down_token_id: str
    winner: str  # up | down | unknown
    up_won: Optional[bool]
    outcome_prices: str
    trade_count: int = 0
    price_points_up: int = 0
    price_points_down: int = 0


def _iso_days_ago(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_json_field(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
    return []


def _winner_from_outcomes(outcomes: list, prices: list) -> tuple[str, Optional[bool]]:
    if not outcomes or not prices or len(outcomes) != len(prices):
        return "unknown", None
    try:
        floats = [float(p) for p in prices]
    except (TypeError, ValueError):
        return "unknown", None
    if max(floats) < 0.5:
        return "unknown", None
    idx = floats.index(max(floats))
    name = str(outcomes[idx]).strip().lower()
    if name in ("up", "yes"):
        return "up", True
    if name in ("down", "no"):
        return "down", False
    return name or "unknown", None


def _parse_window_from_event(ev: dict, spec: dict) -> Optional[WindowRecord]:
    markets = ev.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    outs = _parse_json_field(m.get("outcomes"))
    prices = _parse_json_field(m.get("outcomePrices"))
    toks = _parse_json_field(m.get("clobTokenIds"))
    if len(toks) < 2:
        return None

    w = parse_hourly_event(ev, series_slug=spec["series_slug"],
                          series_label="%s_%s" % (spec["asset"], spec["lane"]))
    if w is None:
        return None

    open_ts = float(w.open_ts)
    close_ts = float(w.close_ts)
    if spec["lane"] == "15m":
        open_ts = close_ts - float(WINDOW_SECONDS_15M)

    winner, up_won = _winner_from_outcomes(outs, prices)
    return WindowRecord(
        event_id=str(ev.get("id") or ""),
        event_slug=str(ev.get("slug") or w.slug),
        market_id=str(m.get("id") or w.market_id),
        condition_id=str(m.get("conditionId") or ""),
        asset=spec["asset"],
        lane=spec["lane"],
        series_slug=spec["series_slug"],
        window_seconds=int(spec["window_seconds"]),
        open_ts=open_ts,
        close_ts=close_ts,
        title=str(ev.get("title") or w.title),
        up_token_id=str(w.up_token_id),
        down_token_id=str(w.down_token_id),
        winner=winner,
        up_won=up_won,
        outcome_prices=json.dumps(prices),
    )


class BackfillClient:
    def __init__(self, *, rate_limit_s: float = 0.2, timeout_s: float = 30.0):
        self._delay = float(rate_limit_s)
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": "bot3-polymarket-backfill/1.0"},
        )
        self._last_req = 0.0

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_req
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_req = time.time()

    def get_json(self, url: str, params: Optional[dict] = None) -> Any:
        self._throttle()
        r = self._client.get(url, params=params or {})
        if r.status_code != 200:
            raise RuntimeError("HTTP %s %s params=%s body=%s" % (
                r.status_code, url, params, r.text[:200]))
        return r.json()

    def list_closed_events(self, series_slug: str, since_iso: str) -> list[dict]:
        """List closed events using weekly date chunks (Gamma offset max ~2000)."""
        since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        out: list[dict] = []
        seen: set[str] = set()
        chunk_start = since_dt
        while chunk_start < now_dt:
            chunk_end = min(chunk_start + timedelta(days=7), now_dt)
            min_iso = chunk_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            max_iso = chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            offset = 0
            while True:
                batch = self.get_json(
                    "%s/events" % GAMMA_API,
                    {
                        "series_slug": series_slug,
                        "closed": "true",
                        "end_date_min": min_iso,
                        "end_date_max": max_iso,
                        "limit": 100,
                        "offset": offset,
                        "order": "endDate",
                        "ascending": "false",
                    },
                )
                if not isinstance(batch, list) or not batch:
                    break
                for ev in batch:
                    slug = str(ev.get("slug") or ev.get("id") or "")
                    if slug and slug not in seen:
                        seen.add(slug)
                        out.append(ev)
                offset += len(batch)
                if len(batch) < 100 or offset >= 1900:
                    break
            chunk_start = chunk_end
        return out

    def fetch_price_history(self, token_id: str) -> list[dict]:
        data = self.get_json(
            "%s/prices-history" % CLOB_API,
            {"market": token_id, "interval": "max", "fidelity": 1},
        )
        return list((data or {}).get("history") or [])

    def fetch_all_trades(self, condition_id: str) -> list[dict]:
        if not condition_id:
            return []
        out: list[dict] = []
        offset = 0
        while True:
            batch = self.get_json(
                "%s/trades" % DATA_API,
                {"market": condition_id, "limit": 500, "offset": offset},
            )
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            offset += len(batch)
            if len(batch) < 500:
                break
        return out


def _load_checkpoint(path: Path) -> dict:
    if not path.exists():
        return {"done": {}, "stats": {"windows": 0, "trades": 0, "errors": 0}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"done": {}, "stats": {"windows": 0, "trades": 0, "errors": 0}}


def _save_checkpoint(path: Path, ckpt: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ckpt, indent=2), encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _append_trades_jsonl(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in trades:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _collect_records_from_raw(raw: Path, specs: list[dict]) -> list[WindowRecord]:
    """Rebuild window list from saved Gamma JSON (supports resume + full curated export)."""
    spec_by_series = {s["series_slug"]: s for s in specs}
    out: list[WindowRecord] = []
    gamma_root = raw / "gamma"
    if not gamma_root.exists():
        return out
    for series_dir in sorted(gamma_root.iterdir()):
        if not series_dir.is_dir():
            continue
        spec = spec_by_series.get(series_dir.name)
        if not spec:
            continue
        for path in sorted(series_dir.glob("*.json")):
            try:
                ev = json.loads(path.read_text(encoding="utf-8"))
                rec = _parse_window_from_event(ev, spec)
                if rec:
                    trade_path = raw / "trades" / ("%s.jsonl" % rec.condition_id)
                    if trade_path.exists():
                        rec.trade_count = sum(1 for ln in trade_path.read_text(encoding="utf-8").splitlines() if ln.strip())
                    for side, tid in (("up", rec.up_token_id), ("down", rec.down_token_id)):
                        pfile = raw / "clob" / "prices" / ("%s.json" % tid)
                        if pfile.exists():
                            n = len(json.loads(pfile.read_text(encoding="utf-8")))
                            if side == "up":
                                rec.price_points_up = n
                            else:
                                rec.price_points_down = n
                    out.append(rec)
            except (json.JSONDecodeError, OSError):
                continue
    return out


def backfill(
    *,
    output: Path,
    days: int,
    with_trades: bool,
    series_filter: Optional[set[str]] = None,
) -> dict:
    output = output.resolve()
    raw = output / "raw"
    curated = output / "curated"
    ckpt_path = output / "checkpoint.json"
    ckpt = _load_checkpoint(ckpt_path)
    done: set[str] = set(ckpt.get("done") or {})
    stats = dict(ckpt.get("stats") or {"windows": 0, "trades": 0, "errors": 0})
    records: list[WindowRecord] = []

    since_iso = _iso_days_ago(days)
    client = BackfillClient()

    try:
        specs = [s for s in SERIES if not series_filter or s["series_slug"] in series_filter]
        log.info("Backfill %sd -> %s (%d series)", days, output, len(specs))

        for spec in specs:
            log.info("Listing closed events: %s", spec["series_slug"])
            events = client.list_closed_events(spec["series_slug"], since_iso)
            log.info("  found %d closed windows", len(events))

            for ev in events:
                rec = _parse_window_from_event(ev, spec)
                if rec is None:
                    continue
                key = rec.event_slug or rec.event_id
                if not key:
                    continue
                trade_path = raw / "trades" / ("%s.jsonl" % rec.condition_id) if rec.condition_id else None
                if key in done:
                    if with_trades and trade_path is not None and not trade_path.exists():
                        log.info("  backfilling trades for %s", key)
                    else:
                        continue

                try:
                    gamma_path = raw / "gamma" / spec["series_slug"] / ("%s.json" % key)
                    if not gamma_path.exists():
                        _write_json(gamma_path, ev)

                    up_path = raw / "clob" / "prices" / ("%s.json" % rec.up_token_id)
                    down_path = raw / "clob" / "prices" / ("%s.json" % rec.down_token_id)
                    if not up_path.exists():
                        up_hist = client.fetch_price_history(rec.up_token_id)
                        _write_json(up_path, up_hist)
                        rec.price_points_up = len(up_hist)
                    else:
                        rec.price_points_up = len(json.loads(up_path.read_text(encoding="utf-8")))
                    if not down_path.exists():
                        down_hist = client.fetch_price_history(rec.down_token_id)
                        _write_json(down_path, down_hist)
                        rec.price_points_down = len(down_hist)
                    else:
                        rec.price_points_down = len(json.loads(down_path.read_text(encoding="utf-8")))

                    if with_trades and rec.condition_id and (trade_path is None or not trade_path.exists()):
                        trades = client.fetch_all_trades(rec.condition_id)
                        rec.trade_count = len(trades)
                        if trade_path is None:
                            trade_path = raw / "trades" / ("%s.jsonl" % rec.condition_id)
                        if trade_path.exists():
                            trade_path.unlink()
                        _append_trades_jsonl(trade_path, trades)
                        stats["trades"] += len(trades)
                    elif trade_path is not None and trade_path.exists():
                        rec.trade_count = sum(1 for ln in trade_path.read_text(encoding="utf-8").splitlines() if ln.strip())

                    records.append(rec)
                    if key not in done:
                        stats["windows"] += 1
                    done.add(key)
                    ckpt["done"] = sorted(done)
                    ckpt["stats"] = stats
                    if stats["windows"] % 25 == 0:
                        _save_checkpoint(ckpt_path, ckpt)
                        log.info("  progress windows=%d trades=%d", stats["windows"], stats["trades"])

                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    log.warning("  failed %s: %s", key, exc)
                    ckpt["stats"] = stats
                    _save_checkpoint(ckpt_path, ckpt)

        curated.mkdir(parents=True, exist_ok=True)
        all_records = _collect_records_from_raw(raw, specs)
        _write_curated(curated, all_records, with_trades)
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "days": days,
            "with_trades": with_trades,
            "series": [s["series_slug"] for s in specs],
            "since_iso": since_iso,
            "stats": stats,
            "output": str(output),
        }
        _write_json(output / "manifest.json", manifest)
        ckpt["manifest"] = manifest
        _save_checkpoint(ckpt_path, ckpt)
        return manifest
    finally:
        client.close()


def _write_curated(curated: Path, records: list[WindowRecord], with_trades: bool) -> None:
    windows_path = curated / "windows.csv"
    prices_path = curated / "prices.csv"
    trades_path = curated / "trades.csv"

    fieldnames = list(asdict(records[0]).keys()) if records else list(WindowRecord.__dataclass_fields__)
    with windows_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for rec in sorted(records, key=lambda r: (r.asset, r.lane, r.close_ts)):
            w.writerow({k: str(v) if v is not None else "" for k, v in asdict(rec).items()})

    window_rows = [{k: str(v) if v is not None else "" for k, v in asdict(rec).items()} for rec in records]

    # Flatten price history
    price_rows: list[dict] = []
    raw_root = curated.parent / "raw"
    for rec in records:
        for side, token_id in (("up", rec.up_token_id), ("down", rec.down_token_id)):
            pfile = raw_root / "clob" / "prices" / ("%s.json" % token_id)
            if not pfile.exists():
                continue
            hist = json.loads(pfile.read_text(encoding="utf-8"))
            for pt in hist:
                price_rows.append({
                    "event_slug": rec.event_slug,
                    "asset": rec.asset,
                    "lane": rec.lane,
                    "side": side,
                    "ts": pt.get("t"),
                    "price": pt.get("p"),
                })

    if price_rows:
        with prices_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["event_slug", "asset", "lane", "side", "ts", "price"])
            w.writeheader()
            w.writerows(price_rows)

    if with_trades:
        trade_rows: list[dict] = []
        for rec in records:
            tfile = raw_root / "trades" / ("%s.jsonl" % rec.condition_id)
            if not tfile.exists():
                continue
            for line in tfile.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                t = json.loads(line)
                trade_rows.append({
                    "event_slug": rec.event_slug,
                    "asset": rec.asset,
                    "lane": rec.lane,
                    "condition_id": rec.condition_id,
                    "timestamp": t.get("timestamp"),
                    "side": t.get("side"),
                    "outcome": t.get("outcome"),
                    "price": t.get("price"),
                    "size": t.get("size"),
                    "tx": t.get("transactionHash"),
                })
        if trade_rows:
            with trades_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=["event_slug", "asset", "lane", "condition_id",
                                "timestamp", "side", "outcome", "price", "size", "tx"],
                )
                w.writeheader()
                w.writerows(trade_rows)

    _write_synthetic_ledger(curated, window_rows)


def _write_synthetic_ledger(curated: Path, window_rows: list[dict]) -> None:
    """Shape minimal settled positions the bot learners can replay."""
    positions = []
    for row in window_rows:
        if not row.get("event_slug"):
            continue
        up_won = row.get("up_won")
        if up_won in ("", None):
            continue
        up_won_b = str(up_won).lower() in ("true", "1")
        for side in ("up", "down"):
            ask = 0.50  # placeholder — learners bucket by band; import uses research fields
            won = up_won_b if side == "up" else (not up_won_b)
            positions.append({
                "status": "settled",
                "side": side,
                "entry_price": ask,
                "won": won,
                "pnl_usd": 2.5 if won else -2.5,
                "entry_ts": float(row.get("close_ts") or 0) - float(row.get("window_seconds") or 900) * 0.5,
                "research": {
                    "series_slug": row.get("series_slug"),
                    "window_seconds": int(float(row.get("window_seconds") or 900)),
                    "entry_ttc_s": float(row.get("window_seconds") or 900) * 0.5,
                    "asset": row.get("asset"),
                    "lane": row.get("lane"),
                    "event_slug": row.get("event_slug"),
                    "source": "polymarket_backfill",
                },
            })

    ledger = {
        "schema": "synthetic_training_ledger/1.0",
        "note": "Generated from Polymarket closed windows — use for offline learner replay",
        "positions": positions,
    }
    _write_json(curated / "synthetic_ledger.json", ledger)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Polymarket BTC/ETH 15m+1h training data")
    parser.add_argument(
        "--output",
        default=os.getenv(
            "BACKFILL_OUTPUT",
            str(ROOT / "data" / "polymarket-training"),
        ),
        help="Output directory (use D:\\polymarket-training on your Samsung T7)",
    )
    parser.add_argument("--days", type=int, default=30, help="Days of closed windows to fetch")
    parser.add_argument("--with-trades", action="store_true", default=True,
                        help="Download market-level trades (default: on)")
    parser.add_argument("--no-trades", action="store_true", help="Skip trade download (faster)")
    parser.add_argument("--series", nargs="*", help="Optional series_slug filter")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    with_trades = bool(args.with_trades) and not bool(args.no_trades)
    series_filter = set(args.series) if args.series else None
    manifest = backfill(
        output=Path(args.output),
        days=args.days,
        with_trades=with_trades,
        series_filter=series_filter,
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
