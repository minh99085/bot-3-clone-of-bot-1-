#!/usr/bin/env python3
"""Roan/Bregman phase gate checker against live VPS status JSON."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
SCORECARD = Path(__file__).resolve().parent / "roan-bregman-promotion-scorecard.json"
DEFAULT_URL = "http://144.202.122.120/api/polymarket/training/btc_pulse"


def _fetch(url: str) -> dict:
    with urlopen(url, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    st = _fetch(url)
    cfg_slugs = st.get("pulse_series_slugs") or []
    dr = st.get("directional_risk") or {}
    ag = st.get("arb_graph") or {}
    bg = st.get("bregman_projection") or {}
    da = st.get("dependency_arbitrage") or {}
    cap = st.get("capital") or {}
    wf = st.get("walk_forward") or {}
    clob = st.get("clob_feed") or {}
    recon = st.get("reconciliation") or {}

    checks = []

    def chk(phase: str, name: str, ok: bool, detail: str = ""):
        checks.append({"phase": phase, "name": name, "ok": ok, "detail": detail})

    chk("1", "dual_scan_slugs",
        "btc-up-or-down-5m" in cfg_slugs and "btc-up-or-down-15m" in cfg_slugs,
        str(cfg_slugs))
    chk("1", "directional_15m_only",
        dr.get("directional_series_slugs") == ["btc-up-or-down-15m"],
        str(dr.get("directional_series_slugs")))
    chk("1", "nested_pairs",
        len(ag.get("nested_pairs") or []) >= 1,
        str(len(ag.get("nested_pairs") or [])))
    chk("1", "global_reconciled", recon.get("global_reconciled") is True)

    chk("2", "bregman_enabled", bg.get("enabled") is True)
    chk("2", "bregman_samples",
        len(bg.get("samples") or []) >= 1 or int(da.get("violations_detected") or 0) > 0,
        f"samples={len(bg.get('samples') or [])} violations={da.get('violations_detected')}")

    chk("3", "fw_configured",
        bool(bg.get("frank_wolfe")),
        str(bg.get("frank_wolfe")))

    chk("4", "dep_execute", da.get("executed", 0) >= 1, f"executed={da.get('executed')}")
    chk("4", "bregman_authority", bg.get("trade_authority") is True)
    chk("4", "segregated", da.get("segregated_from_directional") is True)

    chk("5", "clob_samples", int(clob.get("samples") or 0) > 0,
        f"samples={clob.get('samples')}")

    chk("6", "capital_dep_pnl_field",
        "dependency_arb_realized_pnl_usd" in cap,
        str(cap.get("dependency_arb_realized_pnl_usd")))
    chk("6", "walk_forward_report", bool(wf))
    booking = da.get("booking") or {}
    chk("6", "booking_capture_ratio",
        booking.get("capture_ratio") is not None or da.get("settled", 0) == 0,
        str(booking.get("capture_ratio")))

    failed = [c for c in checks if not c["ok"]]
    out = {
        "verdict": "pass" if not failed else "fail",
        "phases_checked": 6,
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "checks": checks,
        "failed_names": [c["name"] for c in failed],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(out, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())