#!/usr/bin/env python3
"""Audit VPS pulse engine health vs design expectations."""
from __future__ import annotations

import json
import sys
import urllib.request

STATUS_URL = "http://127.0.0.1:8787/api/polymarket/training/btc_pulse"
LIGHT_PATH = "/data/btc_pulse_light_report.json"


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    issues = []
    try:
        st = fetch(STATUS_URL)
    except Exception as exc:  # noqa: BLE001
        print("FAIL: cannot reach pulse status:", exc)
        return 1

    lr = {}
    try:
        from pathlib import Path
        lr = json.loads(Path(LIGHT_PATH).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    print("=== PULSE ENGINE AUDIT ===")
    print("ticks:", st.get("ticks"))
    print("paper_only:", st.get("paper_only"), "live:", st.get("live_trading_enabled"))
    print("global_reconciled:", lr.get("global_reconciled", st.get("global_reconciled")))

    cap = st.get("capital") or {}
    print("total_pnl:", cap.get("total_realized_pnl_usd"),
          "arb:", cap.get("arb_realized_pnl_usd"),
          "dir:", cap.get("realized_pnl_usd"))
    print("primary_edge:", cap.get("primary_edge_source"))

    arb = st.get("arbitrage") or {}
    print("arb executed:", arb.get("executed"), "settled:", arb.get("settled"),
          "scans:", arb.get("arb_scan_count"))

    dep = st.get("dependency_arbitrage") or {}
    print("dep_arb mode:", dep.get("mode"), "violations:", dep.get("violations_detected"))

    cfg = st.get("config") or {}
    print("grok_mode:", cfg.get("grok_decider_mode"), "min_edge:", cfg.get("min_edge"))

    if not lr.get("global_reconciled", True):
        issues.append("global_reconciled=False")

    tv = st.get("tradingview") or {}
    print("\n=== TRADINGVIEW ===")
    print("received:", tv.get("tradingview_alerts_received"),
          "valid:", tv.get("tradingview_alerts_valid"),
          "rejected:", tv.get("tradingview_alerts_rejected"))
    print("reject_reasons:", tv.get("tradingview_reject_reasons"))
    print("feature_symbol:", tv.get("tradingview_feature_symbol"))
    print("mtf_timeframes:", tv.get("tradingview_mtf_timeframes"))

    mtf = tv.get("tradingview_mtf_confirmation") or {}
    windows = mtf.get("confirm_windows_by_tf") or {}
    n = mtf.get("mtf_count") or 5
    print("mtf confirm:", mtf.get("confirm_3tf") or mtf.get("confirm_mtf"),
          "fresh:", mtf.get("trend_fresh_count"), "/", n)
    for tf in ("5", "10", "15"):
        age = mtf.get("tf_%sm_age_s" % tf)
        direc = mtf.get("tf_%sm_dir" % tf)
        win = windows.get(tf)
        stale = age is not None and win is not None and float(age) > float(win)
        flag = "STALE" if stale else ("FRESH" if direc else "MISSING")
        print("  %sm: dir=%s age=%s window=%s [%s]" % (tf, direc, age, win, flag))
        if stale or (age and float(age) > float(win or 0) * 0.8):
            issues.append("tv_%sm_stale_or_aging" % tf)

    rej = tv.get("tradingview_reject_reasons") or {}
    if rej.get("unsupported_symbol"):
        issues.append("tv_unsupported_symbol_rejects=%s" % rej["unsupported_symbol"])
    if rej.get("stale_timestamp"):
        issues.append("tv_stale_timestamp_rejects=%s" % rej["stale_timestamp"])

    loops = st.get("loops") or {}
    print("\n=== LOOPS ===")
    for name in ("arbitrage", "directional", "dependency_arb", "research_meta"):
        beat = (loops.get(name) or {}).get("last_beat_age_s")
        print(" ", name, "last_beat_age_s:", beat)

    print("\n=== ISSUES ===")
    if issues:
        for i in issues:
            print(" -", i)
        return 2
    print(" none detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())