#!/usr/bin/env python3
"""Deep VPS audit: status API, persisted TV state, health, env gaps."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get("HTE_DATA_DIR", "/data"))
EXPECTED_ENV = {
    "PULSE_TV_MTF_TIMEFRAMES": "5,10,15",
    "PULSE_TV_FEATURE_SYMBOL": "BTCUSD",
    "PULSE_GROK_DECIDER_MODE": "shadow",
    "PULSE_ARB_EPSILON": "0.05",
    "PULSE_ARB_NONATOMIC_ENABLED": "1",
    "PULSE_SIZING_PROMOTION_GATED": "1",
    "PULSE_DEPENDENCY_ARB_ENABLED": "1",
    "PULSE_SERIES_SLUGS": "btc-up-or-down-15m",
}


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def read_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8").splitlines():
        if "=" in ln and not ln.strip().startswith("#"):
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> None:
    print("=== HEALTH ===")
    for port, label in ((8787, "training"), (80, "proxy")):
        try:
            h = fetch("http://127.0.0.1:%d/api/health" % port)
            print("%s: fresh=%s age=%s" % (label, h.get("pulse_status_fresh"), h.get("pulse_status_age_s")))
        except Exception as exc:  # noqa: BLE001
            print("%s: FAIL %s" % (label, exc))

    st = fetch("http://127.0.0.1:8787/api/polymarket/training/btc_pulse")
    lr_path = DATA / "btc_pulse_light_report.json"
    lr = json.loads(lr_path.read_text(encoding="utf-8")) if lr_path.exists() else {}

    print("\n=== ENGINE ===")
    print("ticks:", st.get("ticks"), "reconciled:", lr.get("global_reconciled"))
    cap = st.get("capital") or {}
    print("pnl total:", cap.get("total_realized_pnl_usd"), "arb:", cap.get("arb_realized_pnl_usd"))
    arb = st.get("arbitrage") or {}
    print("arb exec:", arb.get("executed"), "scans:", arb.get("arb_scan_count"))

    tv = st.get("tradingview") or {}
    mtf = tv.get("tradingview_mtf_confirmation") or {}
    print("\n=== TV MTF ===")
    print("valid/received:", tv.get("tradingview_alerts_valid"), "/", tv.get("tradingview_alerts_received"))
    print("rejects:", tv.get("tradingview_reject_reasons"))
    print("confirm_3tf:", mtf.get("confirm_3tf"), "fresh:", mtf.get("trend_fresh_count"))
    for tf in ("5", "10", "15"):
        print(" %sm dir=%s age=%s win=%s" % (
            tf, mtf.get("tf_%sm_dir" % tf), mtf.get("tf_%sm_age_s" % tf),
            (mtf.get("confirm_windows_by_tf") or {}).get(tf)))

    tv_path = DATA / "btc_pulse_tradingview.json"
    if tv_path.exists():
        raw = json.loads(tv_path.read_text(encoding="utf-8"))
        print("\n=== TV PERSISTED ===")
        print("reject_reasons:", raw.get("reject_reasons"))
        lbt = raw.get("latest_by_tf") or []
        if isinstance(lbt, dict):
            keys = list(lbt.keys())
        else:
            keys = ["%s@%s" % (a, b) for a, b in (lbt or [])]
        print("latest_by_tf keys:", keys)

    env_path = Path("/opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine/.env")
    env = read_env(env_path)
    print("\n=== ENV GAPS ===")
    for k, exp in EXPECTED_ENV.items():
        got = env.get(k)
        ok = got == exp
        print(("%s OK" if ok else "%s MISSING/WRONG: %s (want %s)") % (k, k, got, exp) if not ok else k)

    status_path = DATA / "btc_pulse_status.json"
    if status_path.exists():
        age = time.time() - status_path.stat().st_mtime
        print("\nstatus.json age_s:", round(age, 1))


if __name__ == "__main__":
    main()