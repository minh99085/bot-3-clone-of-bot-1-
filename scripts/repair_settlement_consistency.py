#!/usr/bin/env python3
"""Rewrite paper settlements so every lane agrees on each window outcome.

Live bug: CEX open/close refs (and per-lane strike meta) differed across
containers, so the same slug+side could settle as a win in one lane and a
loss in another — making fleet PnL disagree with a coherent reading of the
trade history.

This script:
  1. Backs up each lane ledger
  2. Resolves each slug once via ``resolve_window_moved_up`` (Polymarket →
     shared cache → single CEX open/close)
  3. Rewrites settlement ``won`` / ``pnl_usd`` / ``notes`` to match
  4. Prints before/after fleet PnL

Usage (on VPS from repo root)::

    python scripts/repair_settlement_consistency.py --apply
    python scripts/repair_settlement_consistency.py          # dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes.dashboard_data import COMPOSE_LANES, FLEET_BANKROLL  # noqa: E402
from hermes.market_scope import parse_slug, window_step_seconds  # noqa: E402
from hermes.settlement_fast import (  # noqa: E402
    resolve_window_moved_up,
    settlement_pnl_usd,
)


def _paper_root() -> Path:
    return ROOT / "data" / "paper"


def _lane_ids() -> list[str]:
    return [iid for iid, _ in COMPOSE_LANES]


def _load_ledger(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, default=str) for r in rows)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _fleet_pnl(ledgers: dict[str, list[dict]]) -> float:
    total = 0.0
    for rows in ledgers.values():
        for r in rows:
            if r.get("event") == "settlement":
                total += float(r.get("pnl_usd") or 0)
    return round(total, 2)


def _collect_slugs(ledgers: dict[str, list[dict]]) -> dict[str, dict]:
    """slug -> {window_ts, window_end, asset, directions...}"""
    out: dict[str, dict] = {}
    for rows in ledgers.values():
        for r in rows:
            if r.get("event") != "settlement":
                continue
            slug = str(r.get("slug") or "")
            if not slug or slug in out:
                continue
            sm = parse_slug(slug)
            if not sm:
                continue
            end = sm.window_ts + window_step_seconds(sm.timeframe)
            out[slug] = {
                "window_ts": sm.window_ts,
                "window_end": end,
                "asset": sm.asset.upper(),
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write repaired ledgers (default is dry-run)",
    )
    ap.add_argument(
        "--paper-root",
        type=Path,
        default=None,
        help="Override data/paper root",
    )
    args = ap.parse_args()
    root = args.paper_root or _paper_root()

    ledgers: dict[str, list[dict]] = {}
    paths: dict[str, Path] = {}
    for iid in _lane_ids():
        path = root / iid / "trade_ledger.jsonl"
        paths[iid] = path
        ledgers[iid] = _load_ledger(path)

    before = _fleet_pnl(ledgers)
    slugs = _collect_slugs(ledgers)
    print(f"lanes={len(ledgers)} settlements_pnl_before={before:+.2f} unique_slugs={len(slugs)}")

    outcomes: dict[str, tuple[bool, str]] = {}
    unresolved = []
    for slug, meta in sorted(slugs.items()):
        moved_up, note = resolve_window_moved_up(
            slug,
            asset=meta["asset"],
            window_ts=meta["window_ts"],
            window_end=meta["window_end"],
        )
        if moved_up is None:
            unresolved.append(slug)
            print(f"  UNRESOLVED {slug}: {note}")
            continue
        outcomes[slug] = (moved_up, note)
        print(f"  {slug}: moved_up={moved_up} ({note})")

    if unresolved:
        print(f"warning: {len(unresolved)} slugs unresolved — those settlements left unchanged")

    changes = 0
    repaired: dict[str, list[dict]] = {}
    for iid, rows in ledgers.items():
        new_rows = []
        for r in rows:
            if r.get("event") != "settlement":
                new_rows.append(r)
                continue
            slug = str(r.get("slug") or "")
            if slug not in outcomes:
                new_rows.append(r)
                continue
            moved_up, note = outcomes[slug]
            direction = str(r.get("direction") or "DOWN").upper()
            won = moved_up if direction in ("UP", "YES") else (not moved_up)
            size = float(r.get("size_usd") or 0)
            entry = float(r.get("entry_price") or 0.5)
            pnl = settlement_pnl_usd(won=won, size_usd=size, entry_price=entry)
            old_won = bool(r.get("won"))
            old_pnl = float(r.get("pnl_usd") or 0)
            if old_won != won or abs(old_pnl - pnl) > 0.009:
                changes += 1
                print(
                    f"  FIX {iid} {slug} {direction}: "
                    f"won {old_won}->{won} pnl {old_pnl:+.2f}->{pnl:+.2f}"
                )
                r = {
                    **r,
                    "won": won,
                    "pnl_usd": pnl,
                    "exit_price": 1.0 if won else 0.0,
                    "notes": (
                        f"{note} asset={meta_asset(slug)} "
                        f"repaired=1 prev_won={old_won} prev_pnl={old_pnl}"
                    ),
                }
            new_rows.append(r)
        repaired[iid] = new_rows

    after = _fleet_pnl(repaired)
    print(
        f"changes={changes} fleet_pnl {before:+.2f} -> {after:+.2f} "
        f"(equity ${FLEET_BANKROLL + after:,.2f}, bankroll ${FLEET_BANKROLL:,.0f})"
    )

    if not args.apply:
        print("dry-run only — re-run with --apply to write ledgers")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = root / "archive" / f"settle_repair_{stamp}"
    backup_root.mkdir(parents=True, exist_ok=True)
    for iid, path in paths.items():
        if path.is_file():
            dest = backup_root / iid
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest / "trade_ledger.jsonl")
        _write_ledger(path, repaired[iid])
    print(f"wrote repaired ledgers; backups at {backup_root}")
    return 0


def meta_asset(slug: str) -> str:
    sm = parse_slug(slug)
    return sm.asset.upper() if sm else "BTC"


if __name__ == "__main__":
    raise SystemExit(main())
