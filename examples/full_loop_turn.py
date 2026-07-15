"""Example: one full loop turn walkthrough (also runnable).

  PYTHONPATH=. python examples/full_loop_turn.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import os

os.environ["HERMES_FORCE_SYNTHETIC"] = "1"

from hermes.hermes_loop import run_one_turn, simulate_settlement_for_demo


def main() -> None:
    print("=" * 60)
    print("Hermes v2 — one full loop turn (paper)")
    print("data → signal → verify → execute → lesson")
    print("=" * 60)

    result = run_one_turn(paper=True)
    print("\nTurn summary:")
    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))

    print("\nSimulating a settlement so lessons_engine closes the loop...")
    settles = simulate_settlement_for_demo(paper=True)
    print(f"Settlements: {len(settles)} (see knowledge/LESSONS.md)")

    print("\nHandoffs written under data/handoff/")
    handoff = ROOT / "data" / "handoff"
    if handoff.exists():
        for p in sorted(handoff.glob("*.json"))[-8:]:
            print(f"  - {p.name}")


if __name__ == "__main__":
    main()
