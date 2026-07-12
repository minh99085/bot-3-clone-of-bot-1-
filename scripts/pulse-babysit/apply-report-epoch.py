#!/usr/bin/env python3
"""Scope pulled VPS report artifacts to the active report epoch."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine"
sys.path.insert(0, str(ENGINE))

from engine.pulse.report_epoch import (  # noqa: E402
    EPOCH_FILE,
    filter_ledger_doc,
    filter_score_history,
    load_epoch_file,
)


def main() -> int:
    latest = ROOT / "vps_full_reports" / "latest"
    epoch_path = latest / EPOCH_FILE
    if not epoch_path.exists():
        light = latest / "btc_pulse_light_report.json"
        if light.exists():
            try:
                epoch = json.loads(light.read_text(encoding="utf-8")).get("report_epoch")
                if epoch and epoch.get("ts"):
                    epoch_path.write_text(json.dumps(epoch, indent=1), encoding="utf-8")
            except Exception:
                pass
    epoch = load_epoch_file(latest)
    if not epoch or not epoch.get("ts"):
        print("apply-report-epoch: no report epoch — skipping filter")
        return 0

    ledger_path = latest / "btc_pulse_ledger.json"
    if ledger_path.exists():
        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
        filtered = filter_ledger_doc(raw, epoch)
        ledger_path.write_text(json.dumps(filtered, indent=1), encoding="utf-8")
        print("apply-report-epoch: scoped btc_pulse_ledger.json to %s" % epoch.get("utc"))

    score_path = latest / "btc_pulse_score_history.json"
    if score_path.exists():
        raw = json.loads(score_path.read_text(encoding="utf-8"))
        score_path.write_text(
            json.dumps(filter_score_history(raw, epoch), indent=1), encoding="utf-8")
        print("apply-report-epoch: scoped score history")

    epoch_path.write_text(json.dumps(epoch, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
