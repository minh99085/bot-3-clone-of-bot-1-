#!/usr/bin/env python3
"""Deterministic pulse health check for the closed loop. Prints JSON to stdout."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BABYSIT = Path(__file__).resolve().parent
LATEST = ROOT / "vps_full_reports" / "latest"
STATUS = LATEST / "btc_pulse_status.json"
LIGHT = LATEST / "btc_pulse_light_report.json"
LEDGER = LATEST / "btc_pulse_ledger.json"
STATE = Path(__file__).resolve().parent / "state.json"
STARVATION_MIN_HOURS_DEFAULT = 6.0
STARVATION_MIN_HOURS_REAL_MONEY = 3.0
STARVATION_MIN_TICKS = 3
STARVATION_FLAT_EVAL_STREAK = 2

sys.path.insert(0, str(BABYSIT))
try:
    from price_band_analysis import analyze_price_bands, detect_band_issues
except ImportError:
    analyze_price_bands = None  # type: ignore[misc, assignment]
    detect_band_issues = None  # type: ignore[misc, assignment]


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _issue(code: str, severity: str, detail: str, hint: str = "") -> dict:
    return {"code": code, "severity": severity, "detail": detail, "hint": hint}


def main() -> int:
    status = _load(STATUS)
    light = _load(LIGHT)
    if not status and not light:
        out = {
            "verdict": "blocked",
            "healthy": False,
            "issues": [_issue("no_report", "P0", f"missing {STATUS}", "run pull-vps-artifacts.ps1")],
            "metrics": {},
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(out, indent=2))
        return 2

    data = status if status.get("available") is not False else light
    ledger = data.get("ledger") or {}
    capital = data.get("capital") or {}
    tv = data.get("tradingview") or {}
    config = data.get("config") or {}
    reconciled = bool((light or {}).get("global_reconciled", True))

    trades = int(ledger.get("settled") or ledger.get("trades") or 0)
    wr = ledger.get("win_rate")
    pf = ledger.get("profit_factor")
    wr_up = ledger.get("win_rate_up")
    wr_down = ledger.get("win_rate_down")
    pnl = float(capital.get("realized_pnl_usd") or 0.0)

    mtf = tv.get("mtf_gate") or {}
    ctx = tv.get("context_gate") or {}
    dbg = tv.get("down_bias_gate") or {}
    sg = tv.get("signal_gate") or {}
    learning = data.get("learning") or {}

    issues: list[dict] = []

    gd = data.get("grok_decider") or {}
    ver = data.get("verifier") or {}
    stop = data.get("stop_conditions") or {}
    loops = (data.get("loops") or {}).get("loops") or {}

    if gd.get("mode") != "shadow" or gd.get("affects_trading"):
        issues.append(_issue("grok_not_shadow", "P0",
                             f"mode={gd.get('mode')} affects={gd.get('affects_trading')}",
                             "PULSE_GROK_DECIDER_MODE=shadow (Grok must not trade)"))
    if gd.get("mode") == "follow":
        issues.append(_issue("grok_follow_on", "P0", "Grok follow mode is enabled",
                             "set PULSE_GROK_DECIDER_MODE=shadow"))
    if not ver.get("enabled"):
        issues.append(_issue("verifier_disabled", "P0", "verifier.enabled is false",
                             "ANTHROPIC_API_KEY + PULSE_VERIFIER_ENABLED=1; recreate container"))
    if float(gd.get("explore_rate") or 0) > 0:
        issues.append(_issue("grok_explore_on", "P0",
                             f"explore_rate={gd.get('explore_rate')}",
                             "PULSE_GROK_DECIDER_EXPLORE_RATE=0 (coin-flip abstain trades lose)"))
    if float(gd.get("min_confidence") or 0) < 0.62:
        issues.append(_issue("grok_min_conf_low", "P1",
                             f"min_confidence={gd.get('min_confidence')}",
                             "PULSE_GROK_DECIDER_MIN_CONFIDENCE=0.62"))
    if (gd.get("mode") == "follow" and float(gd.get("direction_accuracy") or 1) < 0.52
            and int(gd.get("graded_directional") or 0) >= 20):
        issues.append(_issue("grok_no_edge", "P1",
                             f"direction_accuracy={gd.get('direction_accuracy')}",
                             "Grok at coin-flip — keep shadow or block weak UP"))
    if stop.get("any_halted"):
        strats = stop.get("strategies") or {}
        for name, st in strats.items():
            if not (st or {}).get("halted"):
                continue
            hint = ("inspect stop_conditions or raise PULSE_STOP_MIN_SAMPLES"
                    if name == "directional"
                    else "inspect stop_conditions report for %s" % name)
            issues.append(_issue("strategy_halted", "P0",
                                 "%s halted reasons=%s" % (name, st.get("reasons")),
                                 hint))
    for loop in ("tradingview", "signal_generation", "verifier", "execution"):
        if loop not in loops:
            issues.append(_issue("loop_missing", "P1", f"missing loop {loop}",
                                 "redeploy engine 202d40c+ and restart"))
    if sg.get("enabled"):
        issues.append(_issue("tv_signal_gate_on", "P1", "TV signal gate enabled",
                             "PULSE_TRADINGVIEW_SIGNAL_GATE=0"))
    if mtf.get("require_confirm"):
        issues.append(_issue("mtf_require_confirm_on", "P0", "MTF require_confirm on",
                             "PULSE_TV_MTF_REQUIRE_CONFIRM=0"))
    if mtf.get("require_all_confirm"):
        issues.append(_issue("mtf_require_all_on", "P0", "MTF require_all_confirm on",
                             "PULSE_TV_MTF_REQUIRE_ALL_CONFIRM=0"))
    if ctx.get("enabled"):
        issues.append(_issue("tv_context_gate_on", "P0", "TV context_gate enabled",
                             "PULSE_TV_CONTEXT_GATE=0"))

    if not reconciled:
        issues.append(_issue("reconciliation_broken", "P0", "global_reconciled is false",
                             "fix accounting before tuning gates"))

    st = _load(STATE)
    goals = st.get("goals") or {}
    wr_target = float(goals.get("win_rate_target") or 0.80)
    real_money = goals.get("mode") == "real_money_discipline"
    learning_mode = goals.get("mode") == "learning_collection"
    starvation_min_hours = (STARVATION_MIN_HOURS_REAL_MONEY if real_money
                            else STARVATION_MIN_HOURS_DEFAULT)
    engine_ts = float(data.get("ts") or 0)
    ticks = int(data.get("ticks") or 0)
    dir_halted = bool((stop.get("strategies") or {}).get("directional", {}).get("halted"))

    last_entry_ts = 0.0
    led = _load(LEDGER)
    for pos in led.get("positions") or []:
        try:
            last_entry_ts = max(last_entry_ts, float(pos.get("entry_ts") or pos.get("open_ts") or 0))
        except (TypeError, ValueError):
            pass
    hours_since_trade = ((engine_ts - last_entry_ts) / 3600.0
                           if (engine_ts and last_entry_ts) else None)

    hist = st.get("history") or []
    recent_settled = [int((h.get("metrics") or {}).get("settled") or 0) for h in hist[-6:]]
    settled_flat_streak = 0
    if recent_settled:
        cur = recent_settled[-1]
        for n in reversed(recent_settled):
            if n == cur:
                settled_flat_streak += 1
            else:
                break
        # Break streak when current ledger advanced since last eval (avoids false P0 after soak).
        if trades > cur:
            settled_flat_streak = 0

    trade_starvation = False
    if (not dir_halted and ticks >= STARVATION_MIN_TICKS and hours_since_trade is not None
            and hours_since_trade >= starvation_min_hours):
        trade_starvation = True
        issues.append(_issue(
            "trade_starvation", "P0",
            f"no_new_trades_for_h={hours_since_trade:.1f} settled={trades} ticks={ticks}",
            "ABNORMAL: bot scans but never fills — audit gate stack / relax over-tight blocks; "
            "do NOT add more WR tighten rules until trades resume"))
    elif (not dir_halted and ticks >= STARVATION_MIN_TICKS
          and settled_flat_streak >= STARVATION_FLAT_EVAL_STREAK and trades > 0):
        trade_starvation = True
        issues.append(_issue(
            "trade_starvation_streak", "P0",
            f"settled_unchanged_for_{settled_flat_streak}_evals settled={trades} ticks={ticks}",
            "settled count flat across cycles while bot runs — relax gates or fix deadlock; "
            "never tighten on stale ledger WR alone"))

    if trades >= 10 and not trade_starvation:
        wr_hint = ("tighten quant selectivity / reward_risk — never re-enable TV trade gates")
        if learning_mode:
            wr_hint = ("WR below target — defer tightening while the loop is still collecting "
                       "evidence; prioritize fill rate + ledger continuity")
        if wr is not None and float(wr) < wr_target:
            if learning_mode:
                issues.append(_issue("win_rate_below_target", "P1",
                                     f"win_rate={wr} target={wr_target} settled={trades}",
                                     wr_hint))
            else:
                issues.append(_issue("win_rate_below_target", "P1",
                                     f"win_rate={wr} target={wr_target} settled={trades}",
                                     wr_hint))
        if wr is not None and float(wr) < 0.55 and (real_money or not learning_mode):
            issues.append(_issue("win_rate_low", "P1", f"win_rate={wr}",
                                 "review quant gates / DOWN-only restrictors (TV gates locked off)"))
        if pf is not None and float(pf) < 1.0 and (real_money or not learning_mode):
            issues.append(_issue("profit_factor_low", "P1", f"profit_factor={pf}",
                                 "review min_reward_risk / selectivity — never TV context_gate"))
        if wr_up is not None and wr_down is not None:
            if float(wr_up) < 0.52 and float(wr_down) >= 0.60:
                issues.append(_issue("up_side_bleed", "P1",
                                     f"win_rate_up={wr_up} win_rate_down={wr_down}",
                                     "strengthen DOWN-only restrictors; do not enable TV gates"))

    tv_valid = int(tv.get("tradingview_alerts_valid") or 0)
    if tv.get("enabled") and tv_valid < 5:
        issues.append(_issue("tv_feed_unhealthy", "P2", f"valid_alerts={tv_valid}",
                             "check TradingView webhooks and secret"))

    if (mtf.get("enabled") and mtf.get("require_confirm")
            and int(mtf.get("passed") or 0) == 0 and int(mtf.get("blocked") or 0) >= 20):
        mtf_c = (tv.get("tradingview_mtf_confirmation") or {}).get("confirm")
        issues.append(_issue("mtf_starved", "P2",
                             f"mtf_passed=0 blocked={mtf.get('blocked')} confirm={mtf_c}",
                             "MTF trade gate must stay off — check INDEX:BTCUSD 2m/3m/4m webhook health only"))

    bench = learning.get("market_benchmark") or {}
    if learning.get("active") and bench.get("model_beats_market") is False:
        issues.append(_issue("learning_hurts", "P2",
                             f"model_brier={bench.get('model_brier')} market={bench.get('market_brier')}",
                             "veto learning blend when model_not_beating_market"))

    price_band_24h: dict = {}
    if analyze_price_bands and detect_band_issues and not trade_starvation:
        try:
            price_band_24h = analyze_price_bands(
                led,
                lookback_hours=24.0,
                side="down",
                now_ts=engine_ts if engine_ts else None,
            )
            for bi in detect_band_issues(price_band_24h):
                issues.append(_issue(
                    bi["code"], bi["severity"], bi["detail"], bi.get("hint", ""),
                ))
        except Exception:
            price_band_24h = {}

    sev_order = {"P0": 0, "P1": 1, "P2": 2}
    issues.sort(key=lambda x: sev_order.get(x["severity"], 9))

    healthy = len(issues) == 0
    verdict = "healthy" if healthy else ("blocked" if any(i["severity"] == "P0" for i in issues) else "issues")

    metrics = {
        "settled": trades,
        "win_rate_target": wr_target,
        "win_rate": wr,
        "profit_factor": pf,
        "win_rate_up": wr_up,
        "win_rate_down": wr_down,
        "realized_pnl_usd": round(pnl, 2),
        "tv_valid": tv_valid,
        "mtf_passed": mtf.get("passed"),
        "mtf_blocked": mtf.get("blocked"),
        "context_blocked": ctx.get("blocked"),
        "down_bias_blocked": dbg.get("blocked"),
        "signal_gate": sg.get("enabled"),
        "min_reward_risk": config.get("min_reward_risk"),
        "global_reconciled": reconciled,
        "ticks": ticks,
        "hours_since_last_trade": (round(hours_since_trade, 2)
                                   if hours_since_trade is not None else None),
        "settled_flat_eval_streak": settled_flat_streak,
        "price_band_24h": price_band_24h,
    }

    out = {
        "verdict": verdict,
        "healthy": healthy,
        "issues": issues,
        "metrics": metrics,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(out, indent=2))

    # append to state history
    try:
        st["last_eval_at"] = out["ts"]
        st["last_verdict"] = verdict
        hist = st.setdefault("history", [])
        hist.append({"ts": out["ts"], "verdict": verdict, "metrics": metrics,
                     "issue_codes": [i["code"] for i in issues]})
        st["history"] = hist[-100:]
        STATE.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass

    try:
        import subprocess
        summary_script = Path(__file__).resolve().parent / "write-cycle-summary.py"
        if summary_script.exists():
            subprocess.run([sys.executable, str(summary_script)], check=False, timeout=30)
    except Exception:
        pass

    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())