#!/usr/bin/env python3
"""Loop-engine synthesis CLI (WS5): read the latest light report -> ranked next-experiment proposals.

Advisory only. Reads vps_full_reports/latest/btc_pulse_light_report.json by default (override with a
path arg), prints a ranked proposal list, and writes loop_synthesis.json next to the report. NEVER
edits config or trades — it surfaces the next minimal experiment for the operator / Grok to weigh.

Usage:
    python3 scripts/loop-synthesis.py [path/to/btc_pulse_light_report.json] [--json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine"
sys.path.insert(0, str(ENGINE_ROOT))

from engine.pulse.loop_synthesis import synthesize  # noqa: E402

DEFAULT_REPORT = ROOT / "vps_full_reports" / "latest" / "btc_pulse_light_report.json"


def main(argv: list) -> int:
    args = [a for a in argv if not a.startswith("--")]
    as_json = "--json" in argv
    path = Path(args[0]) if args else DEFAULT_REPORT
    if not path.exists():
        print("report not found: %s" % path, file=sys.stderr)
        return 2
    report = json.loads(path.read_text())
    out = synthesize(report)

    if as_json:
        print(json.dumps(out, indent=2))
    else:
        print("=== Loop synthesis (%d proposals) ===" % out["proposal_count"])
        print(out["summary"])
        print()
        for i, p in enumerate(out["proposals"], 1):
            print("%d. [%s] %s" % (i, p["priority"].upper(), p["area"]))
            print("   observe:  %s" % p["observation"])
            print("   propose:  %s" % p["proposed_change"])
            print("   gate:     %s" % p["evidence_gate"])
            print()

    try:
        (path.parent / "loop_synthesis.json").write_text(json.dumps(out, indent=2))
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
