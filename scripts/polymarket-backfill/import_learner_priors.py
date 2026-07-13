#!/usr/bin/env python3
"""Import offline-replay learner priors into a bot data directory (VPS /data).

Merges directional_cell_learning into BOTH the standalone JSON file AND
btc_pulse_ledger.json accounting_state (required — ledger overwrites file on boot).
Also copies lane_15m prior and applies its policy into the ledger when present.

Usage:
  python3 scripts/polymarket-backfill/import_learner_priors.py \\
      --replay /data/polymarket-training/replay \\
      --data-dir /data
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _merge_cells(live: dict, offline: dict) -> dict:
    """Warm-start merge: keep the richer cell row per key (by trades), never double-count."""
    live_cells = dict((live or {}).get("cells") or {})
    off_cells = dict((offline or {}).get("cells") or {})
    for key, stats in off_cells.items():
        if not isinstance(stats, dict):
            continue
        cur = live_cells.get(key)
        off_trades = int(stats.get("trades", 0) or 0)
        if cur is None or int(cur.get("trades", 0) or 0) < off_trades:
            live_cells[key] = {
                "evals": int(stats.get("evals", 0) or 0),
                "trades": off_trades,
                "wins": int(stats.get("wins", 0) or 0),
                "pnl_usd": round(float(stats.get("pnl_usd", 0) or 0), 4),
            }
    return {"schema": "directional_cell_learning/2.0", "cells": live_cells}


def _patch_ledger(
    data_dir: Path,
    *,
    cell_state: dict | None,
    lane_state: dict | None,
    stamp: str,
    dry_run: bool,
) -> dict | None:
    """Merge offline priors into live ledger accounting_state so restart loads them."""
    ledger_path = data_dir / "btc_pulse_ledger.json"
    if not ledger_path.exists():
        return {"skipped": "no_ledger"}
    ledger = _load(ledger_path)
    acct = dict(ledger.get("accounting_state") or {})
    actions: dict = {}

    if cell_state is not None:
        merged = _merge_cells(acct.get("cell_learning") or {}, cell_state)
        acct["cell_learning"] = merged
        actions["cell_cells"] = len(merged.get("cells") or {})

    if lane_state is not None:
        live_lane = dict(acct.get("lane_15m_learner") or {})
        # Prefer offline-learned policy knobs; keep live rolling window if present.
        off_pol = (lane_state.get("policy") or {})
        live_pol = dict(live_lane.get("policy") or {})
        live_pol.update(off_pol)
        live_lane["policy"] = live_pol
        if not live_lane.get("recent") and lane_state.get("recent"):
            live_lane["recent"] = list(lane_state.get("recent") or [])[-64:]
        live_lane["offline_prior_imported_at"] = stamp
        live_lane["last_action"] = live_lane.get("last_action") or "offline_prior_import"
        acct["lane_15m_learner"] = live_lane
        actions["lane_policy"] = live_pol

    ledger["accounting_state"] = acct
    actions["ledger"] = str(ledger_path)
    if not dry_run:
        bak = data_dir / ("btc_pulse_ledger.pre_import_%s.json" % stamp)
        shutil.copy2(ledger_path, bak)
        ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        actions["backup"] = str(bak)
    return actions


def import_priors(*, replay_dir: Path, data_dir: Path, dry_run: bool = False) -> dict:
    replay_dir = replay_dir.resolve()
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    lane_src = replay_dir / "lane_15m_learner.json"
    cell_src = replay_dir / "directional_cell_learning.json"
    report_src = replay_dir / "walk_forward_report.json"

    if not lane_src.exists() and not cell_src.exists():
        raise SystemExit("No learner files in %s — run replay_offline.py first" % replay_dir)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    actions = []
    merged_cells = None
    lane_state = None

    if cell_src.exists():
        live_path = data_dir / "directional_cell_learning.json"
        offline = _load(cell_src)
        live = _load(live_path) if live_path.exists() else {}
        merged_cells = _merge_cells(live, offline)
        actions.append({"file": str(live_path), "cells": len(merged_cells["cells"])})
        if not dry_run:
            if live_path.exists():
                shutil.copy2(live_path, data_dir / ("directional_cell_learning.pre_import_%s.json" % stamp))
            live_path.write_text(json.dumps(merged_cells, indent=2), encoding="utf-8")

    if lane_src.exists():
        dest = data_dir / "lane_15m_learner_offline_prior.json"
        lane_state = _load(lane_src)
        actions.append({"file": str(dest), "bytes": lane_src.stat().st_size})
        if not dry_run:
            shutil.copy2(lane_src, dest)

    if report_src.exists() and not dry_run:
        shutil.copy2(report_src, data_dir / "offline_walk_forward_report.json")

    ledger_action = _patch_ledger(
        data_dir,
        cell_state=merged_cells or (_load(cell_src) if cell_src.exists() else None),
        lane_state=lane_state,
        stamp=stamp,
        dry_run=dry_run,
    )
    if ledger_action:
        actions.append({"ledger_patch": ledger_action})

    manifest = {
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "replay_dir": str(replay_dir),
        "data_dir": str(data_dir),
        "dry_run": dry_run,
        "actions": actions,
        "note": (
            "Ledger accounting_state patched so hermes-training restart loads offline "
            "cell + lane priors. Restart training container after import."
        ),
    }
    if not dry_run:
        (data_dir / "offline_import_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Import offline learner priors into bot data dir")
    parser.add_argument("--replay", required=True, help="Path to replay/ output folder")
    parser.add_argument("--data-dir", required=True, help="Bot HTE_DATA_DIR (e.g. /data)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = import_priors(
        replay_dir=Path(args.replay),
        data_dir=Path(args.data_dir),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
