#!/usr/bin/env python3
"""Import offline-replay learner priors into a bot data directory (VPS /data).

Merges lane_15m_learner + directional_cell_learning from a replay/ folder into
the live Pulse data dir WITHOUT wiping the live paper ledger.

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
    """Add offline cell trade counts into live cells (additive warm-start)."""
    live_cells = dict((live or {}).get("cells") or live or {})
    off_cells = dict((offline or {}).get("cells") or offline or {})
    for key, stats in off_cells.items():
        cur = live_cells.get(key) or {"evals": 0, "trades": 0, "wins": 0, "pnl_usd": 0.0}
        live_cells[key] = {
            "evals": int(cur.get("evals", 0)) + int(stats.get("evals", 0)),
            "trades": int(cur.get("trades", 0)) + int(stats.get("trades", 0)),
            "wins": int(cur.get("wins", 0)) + int(stats.get("wins", 0)),
            "pnl_usd": round(float(cur.get("pnl_usd", 0)) + float(stats.get("pnl_usd", 0)), 4),
        }
    return {"schema": "directional_cell_learning/2.0", "cells": live_cells}


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

    if cell_src.exists():
        live_path = data_dir / "directional_cell_learning.json"
        offline = _load(cell_src)
        live = _load(live_path) if live_path.exists() else {}
        merged = _merge_cells(live, offline)
        actions.append({"file": str(live_path), "cells": len(merged["cells"])})
        if not dry_run:
            if live_path.exists():
                shutil.copy2(live_path, data_dir / ("directional_cell_learning.pre_import_%s.json" % stamp))
            live_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")

    if lane_src.exists():
        # Keep offline prior as sidecar; live lane state lives in ledger accounting_state.
        dest = data_dir / "lane_15m_learner_offline_prior.json"
        actions.append({"file": str(dest), "bytes": lane_src.stat().st_size})
        if not dry_run:
            shutil.copy2(lane_src, dest)

    if report_src.exists() and not dry_run:
        shutil.copy2(report_src, data_dir / "offline_walk_forward_report.json")

    manifest = {
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "replay_dir": str(replay_dir),
        "data_dir": str(data_dir),
        "dry_run": dry_run,
        "actions": actions,
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
