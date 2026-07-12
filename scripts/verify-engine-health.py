#!/usr/bin/env python3
"""Post-deploy health check for Hermes BTC Pulse (run on VPS inside training container)."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get("HTE_DATA_DIR", "/data"))
STATUS_URL = "http://127.0.0.1:8787/api/polymarket/training/btc_pulse"
HEALTH_URL = "http://127.0.0.1:8787/api/health"
FAIL = 1
WARN = 2


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    issues: list[str] = []
    warns: list[str] = []

    try:
        h = fetch(HEALTH_URL)
    except Exception as exc:  # noqa: BLE001
        print("FAIL health endpoint:", exc)
        return FAIL
    if not h.get("pulse_status_fresh"):
        issues.append("pulse_status_stale age=%s" % h.get("pulse_status_age_s"))
    if h.get("live_trading_enabled"):
        issues.append("live_trading_enabled=True")

    st = fetch(STATUS_URL)
    lr_path = DATA / "btc_pulse_light_report.json"
    lr = json.loads(lr_path.read_text(encoding="utf-8")) if lr_path.exists() else {}
    if not lr.get("global_reconciled", True):
        issues.append("global_reconciled=False")

    if not st.get("paper_only", True):
        issues.append("paper_only=False")

    cap = st.get("capital") or {}
    if cap.get("primary_edge_source") not in ("arbitrage", "dependency_arb", "directional", "none"):
        warns.append("unexpected primary_edge_source=%s" % cap.get("primary_edge_source"))

    arb = st.get("arbitrage") or {}
    if int(arb.get("arb_scan_count") or 0) < 1 and int(st.get("ticks") or 0) > 10:
        warns.append("arb_scan_count=0")

    cfg = st.get("config") or {}
    if str(cfg.get("grok_decider_mode")).lower() not in ("shadow", "off"):
        warns.append("grok_decider_mode=%s (expected shadow)" % cfg.get("grok_decider_mode"))

    tv = st.get("tradingview") or {}
    rej = tv.get("tradingview_reject_reasons") or {}
    if rej.get("unsupported_symbol"):
        warns.append("tv_unsupported_symbol=%s" % rej["unsupported_symbol"])
    if rej.get("stale_timestamp"):
        warns.append("tv_stale_timestamp=%s" % rej["stale_timestamp"])

    mtf = tv.get("tradingview_mtf_confirmation") or {}
    windows = mtf.get("confirm_windows_by_tf") or {}
    fresh = int(mtf.get("trend_fresh_count") or 0)
    n = int(mtf.get("mtf_count") or 5)
    if fresh < n:
        stale_tfs = []
        for tf in mtf.get("mtf_timeframes") or ("5", "10", "15"):
            age = mtf.get("tf_%sm_age_s" % tf)
            win = windows.get(str(tf))
            if mtf.get("tf_%sm_dir" % tf) is None and age is not None and win is not None:
                if float(age) > float(win):
                    stale_tfs.append("%sm(%ds)" % (tf, int(float(age))))
            elif mtf.get("tf_%sm_dir" % tf) is None:
                stale_tfs.append("%sm(missing)" % tf)
        warns.append("tv_mtf_fresh=%d/%d stale=%s" % (fresh, n, ",".join(stale_tfs) or "?"))

    print("OK ticks=%s reconciled=%s total_pnl=%s arb_pnl=%s arb_exec=%s" % (
        st.get("ticks"), lr.get("global_reconciled"),
        cap.get("total_realized_pnl_usd"), cap.get("arb_realized_pnl_usd"),
        arb.get("executed")))
    print("TV valid=%s/%s fresh=%s/%s confirm=%s" % (
        tv.get("tradingview_alerts_valid"), tv.get("tradingview_alerts_received"),
        fresh, n, mtf.get("confirm_3tf") or mtf.get("confirm_mtf")))

    if warns:
        print("WARN:")
        for w in warns:
            print(" ", w)
    if issues:
        print("FAIL:")
        for i in issues:
            print(" ", i)
        return FAIL
    return WARN if warns else 0


if __name__ == "__main__":
    sys.exit(main())