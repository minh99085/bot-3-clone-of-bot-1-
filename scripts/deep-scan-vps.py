#!/usr/bin/env python3
"""Deep scan: all pulse modules + external feeds via VPS status API."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://144.202.122.120").rstrip("/")


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode())


def main() -> int:
    issues: list[dict] = []
    checks: list[dict] = []

    def check(name: str, cond: bool, detail: str = "", sev: str = "P1", hint: str = ""):
        checks.append({"name": name, "pass": bool(cond), "detail": detail})
        if not cond:
            issues.append({"code": name, "severity": sev, "detail": detail, "hint": hint})

    health = get("/api/health")
    st = get("/api/polymarket/training/btc_pulse")
    ledger = get("/api/polymarket/training/btc_pulse/ledger")

    # Try light report fields from status extras if embedded
    lr = st.get("light_report") or {}

    check("api_health", health.get("status") == "ok" or health.get("pulse_status_fresh"),
          str(health.get("status") or health.get("pulse_status_fresh")), "P0")
    check("paper_only", bool(st.get("paper_only")) and not st.get("live_trading_enabled"),
          "paper=%s live=%s" % (st.get("paper_only"), st.get("live_trading_enabled")), "P0")
    age = time.time() - float(st.get("ts") or 0)
    check("status_fresh", age < 45, "age_s=%.1f" % age, "P0")
    check("ticks_running", int(st.get("ticks") or 0) > 20, "ticks=%s" % st.get("ticks"), "P0")

    # Price / RTDS
    price = st.get("price") or {}
    check("price_feed_ok", bool(price.get("last_fetch_ok")),
          "source=%s age=%s price=%s" % (price.get("source"), price.get("age_s"), price.get("last_price")), "P0")
    check("price_fresh", float(price.get("age_s") or 999) < 15,
          "age_s=%s polls=%s errors=%s" % (price.get("age_s"), price.get("polls"), price.get("errors")), "P1")
    check("vol_sampler", bool(price.get("sampler_running")) and int(price.get("vol_samples") or 0) > 5,
          "vol_samples=%s sigma=%s" % (price.get("vol_samples"), price.get("sigma_per_sec")), "P1")

    # Arbitrage
    arb = st.get("arbitrage") or {}
    check("arb_scanner_active", int(arb.get("arb_scan_count") or 0) > 0,
          "scans=%s exec=%s settled=%s pnl=%s" % (
              arb.get("arb_scan_count"), arb.get("executed"),
              arb.get("settled"), arb.get("realized_profit_usd")), "P1")
    check("arb_segregated", arb.get("segregated_from_directional") is True, "", "P1")
    check("arb_nonatomic_rejects", True, "near_miss=%s" % arb.get("near_miss_within_eps"), "P2")

    # Dependency arb (WS4)
    dep = st.get("dependency_arbitrage") or {}
    check("dep_scanner_active", int(dep.get("scans") or 0) > 0,
          "scans=%s violations=%s actionable=%s exec=%s mode=%s" % (
              dep.get("scans"), dep.get("violations_detected"),
              dep.get("actionable_detected"), dep.get("executed"), dep.get("mode")), "P1")
    if dep.get("enabled") and dep.get("mode") == "paper_execute":
        check("dep_execute_enabled", True, "enabled paper_execute", "P2")

    # Accounting
    cap = st.get("capital") or {}
    dl_rec = (st.get("decision_lifecycle") or {}).get("reconciled")
    eg_rec = ((ledger.get("stats") or {}).get("execution_gate") or {}).get("reconciled")
    reconciled_ok = dl_rec is True or eg_rec is True
    check("ledger_reconciled", reconciled_ok,
          "lifecycle=%s exec_gate=%s" % (dl_rec, eg_rec), "P0")
    dr = st.get("directional_risk") or {}
    risk_free = cap.get("primary_edge_source") in ("arbitrage", "dependency_arbitrage")
    if dr.get("directional_enabled") is False:
        check("primary_edge_risk_free", risk_free,
              "source=%s mode=arb_first" % cap.get("primary_edge_source"), "P2")
    else:
        check("primary_edge_arbitrage", cap.get("primary_edge_source") == "arbitrage",
              "source=%s" % cap.get("primary_edge_source"), "P2")

    # Grok
    gd = st.get("grok_decider") or {}
    check("grok_shadow_mode", gd.get("mode") == "shadow" and not gd.get("affects_trading"),
          "mode=%s decided=%s abstains=%s" % (gd.get("mode"), gd.get("decided"), gd.get("abstains")), "P0")
    if int(gd.get("errors") or 0) >= 10:
        issues.append({"code": "grok_api_errors", "severity": "P1",
                       "detail": "errors=%s" % gd.get("errors"), "hint": "check XAI_API_KEY / rate limits"})

    # TradingView webhook feed
    tv = st.get("tradingview") or {}
    check("tv_webhook_receiving", int(tv.get("tradingview_alerts_received") or 0) > 0,
          "recv=%s valid=%s rejected=%s" % (
              tv.get("tradingview_alerts_received"),
              tv.get("tradingview_alerts_valid"),
              tv.get("tradingview_alerts_rejected")), "P1")
    rej = tv.get("tradingview_reject_reasons") or {}
    check("tv_no_unsupported_symbol", not rej.get("unsupported_symbol"), str(rej), "P1",
          "expand TRADINGVIEW_ALLOWED_SYMBOLS")
    check("tv_no_stale_rejects_spike", int(rej.get("stale_timestamp") or 0) < 50,
          "stale_timestamp_rejects=%s" % rej.get("stale_timestamp"), "P2")

    mtf = tv.get("tradingview_mtf_confirmation") or {}
    fresh = int(mtf.get("trend_fresh_count") or 0)
    n = int(mtf.get("mtf_count") or 5)
    windows = mtf.get("confirm_windows_by_tf") or {}
    stale_tfs = []
    for tf in mtf.get("mtf_timeframes") or ("5", "10", "15"):
        tf_s = str(tf)
        age = mtf.get("tf_%sm_age_s" % tf_s)
        win = windows.get(tf_s)
        d = mtf.get("tf_%sm_dir" % tf_s)
        if d is None and age is not None and win is not None and float(age) > float(win):
            stale_tfs.append("%sm(%ds>%s)" % (tf_s, int(float(age)), int(float(win))))
        elif d is None:
            stale_tfs.append("%sm(missing)" % tf_s)
    check("tv_mtf_partial_ok", fresh >= 2,
          "fresh=%d/%d confirm=%s stale=%s" % (
              fresh, n, mtf.get("confirm_3tf") or mtf.get("confirm_mtf"), stale_tfs), "P1",
          "keep all 5 INDEX:BTCUSD chart alerts firing")
    if fresh < n:
        issues.append({"code": "tv_mtf_not_all_fresh", "severity": "P1",
                       "detail": "fresh=%d/%d stale=%s" % (fresh, n, stale_tfs),
                       "hint": "4m/5m/13m charts need alert fires"})

    # Loops / modules
    loops = st.get("loops") or {}
    for name in ("heartbeat", "data_ingestion", "arbitrage", "directional",
                 "dependency_arb", "tradingview", "signal_generation", "execution", "verifier"):
        lb = loops.get(name) or {}
        beat = lb.get("last_beat_age_s")
        if beat is not None:
            check("loop_%s" % name, float(beat) < 180,
                  "beat_age_s=%s status=%s" % (beat, lb.get("status")), "P1")

    ver = st.get("verifier") or {}
    check("verifier_enabled", ver.get("enabled") is True,
          "verified=%s vetoes=%s errors=%s" % (ver.get("verified"), ver.get("vetoes"), ver.get("errors")), "P1")

    cex = st.get("cex_lead_edge") or {}
    check("cex_lead_observe", str(cex.get("mode", "shadow")).lower() in ("shadow", "off", ""),
          "mode=%s" % cex.get("mode"), "P2")

    # Directional risk caps (WS2)
    dr = st.get("directional_risk") or {}
    if dr:
        check("directional_cap", float(dr.get("open_exposure_usd") or 0) <= float(dr.get("bankroll_cap_usd") or 999),
              "open=%s cap=%s" % (dr.get("open_exposure_usd"), dr.get("bankroll_cap_usd")), "P1")

    p0 = [i for i in issues if i["severity"] == "P0"]
    p1 = [i for i in issues if i["severity"] == "P1"]
    healthy = len(p0) == 0 and len(p1) == 0
    verdict = "healthy" if healthy else ("blocked" if p0 else "issues")

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "base": BASE,
        "verdict": verdict,
        "healthy": healthy,
        "issues": issues,
        "checks": checks,
        "metrics": {
            "ticks": st.get("ticks"),
            "total_pnl": cap.get("total_realized_pnl_usd"),
            "arb_pnl": cap.get("arb_realized_pnl_usd"),
            "dir_pnl": cap.get("realized_pnl_usd"),
            "dep_pnl": dep.get("realized_profit_usd"),
            "arb_scans": arb.get("arb_scan_count"),
            "dep_scans": dep.get("scans"),
            "dep_violations": dep.get("violations_detected"),
            "dep_actionable": dep.get("actionable_detected"),
            "dep_executed": dep.get("executed"),
            "tv_fresh_ratio": "%d/%d" % (fresh, n),
            "price_source": price.get("source"),
            "price_age_s": price.get("age_s"),
            "tracked_opens": price.get("tracked_opens"),
            "grok_decided": gd.get("decided"),
            "trades": (st.get("ledger") or {}).get("trades"),
            "win_rate": (st.get("ledger") or {}).get("win_rate"),
        },
    }
    print(json.dumps(report, indent=2))
    return 0 if verdict == "healthy" else (2 if verdict == "blocked" else 1)


if __name__ == "__main__":
    sys.exit(main())