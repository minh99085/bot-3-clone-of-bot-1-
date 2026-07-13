#!/usr/bin/env python3
"""Enrich Polymarket backfill into training ledger + walk-forward replay report.

Usage:
  python3 scripts/polymarket-backfill/replay_offline.py \\
      --data /workspace/data/polymarket-training

  python3 scripts/polymarket-backfill/replay_offline.py \\
      --data /data/polymarket-training --modes mid early late
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from engine.pulse.offline_replay import ENTRY_FRACS, run_pipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Polymarket training replay")
    parser.add_argument("--data", required=True, help="Backfill root (contains raw/gamma)")
    parser.add_argument("--out", default=None, help="Output dir (default: <data>/replay)")
    parser.add_argument(
        "--modes", nargs="+", default=["mid"],
        choices=list(ENTRY_FRACS.keys()),
        help="Entry timing modes",
    )
    parser.add_argument("--holdout", type=float, default=0.30, help="Holdout fraction")
    parser.add_argument("--size", type=float, default=5.0, help="Simulated size USD")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    report = run_pipeline(
        Path(args.data),
        out_dir=Path(args.out) if args.out else None,
        entry_modes=tuple(args.modes),
        holdout_fraction=float(args.holdout),
        size_usd=float(args.size),
    )
    # Print compact summary
    summary = {
        "n_windows": report.get("n_windows"),
        "n_positions": report.get("n_positions"),
        "train": report.get("train", {}).get("summary"),
        "train_favorites": report.get("train", {}).get("favorites"),
        "holdout": report.get("holdout", {}).get("all"),
        "holdout_favorites": report.get("holdout", {}).get("favorites"),
        "recommendation": report.get("recommendation"),
        "artifacts": str(Path(args.out) if args.out else Path(args.data) / "replay"),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
