#!/usr/bin/env python3
"""Full pulse bot health scan — runtime + loop-arch invariants. Prints JSON."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BASE = "http://144.202.122.120"


def _issue(code: str, severity: str, detail: str, hint: str = "") -> dict:
    return {"code": code, "severity": severity, "detail": detail, "hint": hint}


def _fetch(url: str, timeout: float = 15.0) -> dict:
    with urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    issues: list[dict] = []
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from validate_frozen_lock import validate_env, _parse_env, MANIFEST as _FM
        _local_env = _parse_env(ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine" / ".env")
        if _FM.exists() and _local_env:
            _mf = json.loads(_FM.read_text(encoding="utf-8"))
            for iss in validate_env(_local_env, _mf):
                if iss["severity"] == "P0":
                    issues.append(iss)
    except Exception:
        pass

    try:
        health = _fetch(f"{base}/api/health")
        status = _fetch(f"{base}/api/polymarket/training/btc_pulse")
        ledger = _fetch(f"{base}/api/polymarket/training/btc_pulse/ledger")
    except Exception as exc:
        out = {
            "verdict": "blocked",
            "healthy": False,
            "issues": [_issue("unreachable", "P0", str(exc), "check VPS / docker")],
            "checks": [],
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(out, indent=2))
        return 2

    record("api_health", health.get("status") == "ok" or health.get("pulse") is not None,
           str(health.get("status") or health.get("pulse") or health))
    record("status_available", status.get("available") is True)

    ts = float(status.get("ts") or 0)
    age = max(0.0, time.time() - ts) if ts else 9999.0
    record("status_fresh", age < 45, f"age_s={age:.1f}")
    record("ticks_positive", int(status.get("ticks") or 0) > 0, f"ticks={status.get('ticks')}")

    gd = status.get("grok_decider") or {}
    if not record("grok_shadow", gd.get("mode") == "shadow" and not gd.get("affects_trading"),
                  f"mode={gd.get('mode')} affects={gd.get('affects_trading')}"):
        issues.append(_issue("grok_not_shadow", "P0", f"mode={gd.get('mode')} affects={gd.get('affects_trading')}",
                             "set PULSE_GROK_DECIDER_MODE=shadow on VPS"))
    if gd.get("mode") == "follow" and gd.get("affects_trading"):
        issues.append(_issue("grok_follow_on", "P0", "Grok is driving trades",
                             "set PULSE_GROK_DECIDER_MODE=shadow"))
    # Observe-only decider: only flag a HIGH failure RATE (systemic API/key/timeout failure), not the
    # lifetime cumulative counter (which a long-lived bot always exceeds via transient timeouts).
    _gd_req = int(gd.get("requested") or 0)
    _gd_err = int(gd.get("errors") or 0)
    if _gd_req >= 20 and _gd_err >= _gd_req * 0.5:
        issues.append(_issue(
            "grok_errors", "P1",
            f"errors={_gd_err}/{_gd_req} ({_gd_err / _gd_req:.0%}) avg_latency_s={gd.get('avg_latency_s')}",
            "high grok failure rate — check XAI_API_KEY, raise PULSE_GROK_DECIDER_TIMEOUT_S, "
            "or set PULSE_GROK_DECIDER_USE_SEARCH=0"))

    ver = status.get("verifier") or {}
    if not record("verifier_enabled", ver.get("enabled") is True, f"enabled={ver.get('enabled')}"):
        issues.append(_issue("verifier_disabled", "P0", "verifier.enabled is false",
                             "set ANTHROPIC_API_KEY + PULSE_VERIFIER_ENABLED=1; recreate container"))
    record("verifier_no_errors", int(ver.get("errors") or 0) == 0,
           f"verified={ver.get('verified')} vetoes={ver.get('vetoes')}")

    tv = status.get("tradingview") or {}
    mg = tv.get("mtf_gate") or {}
    sg = tv.get("signal_gate") or {}
    record("tv_webhook", tv.get("enabled") is True,
           f"valid={tv.get('tradingview_alerts_valid')} rejected={tv.get('tradingview_alerts_rejected')}")
    if sg.get("enabled"):
        issues.append(_issue("tv_signal_gate_on", "P1", "signal_gate enabled",
                             "set PULSE_TRADINGVIEW_SIGNAL_GATE=0 for loop-arch"))
    if mg.get("require_confirm"):
        issues.append(_issue("mtf_require_confirm_on", "P0", "require_confirm=true",
                             "set PULSE_TV_MTF_REQUIRE_CONFIRM=0"))
    if mg.get("require_all_confirm"):
        issues.append(_issue("mtf_require_all_on", "P0", "require_all_confirm=true",
                             "set PULSE_TV_MTF_REQUIRE_ALL_CONFIRM=0"))
    cg = tv.get("context_gate") or {}
    if cg.get("enabled"):
        issues.append(_issue("tv_context_gate_on", "P0", "context_gate enabled",
                             "set PULSE_TV_CONTEXT_GATE=0"))
    record("tv_observe_only", not sg.get("enabled") and not mg.get("require_confirm")
           and not mg.get("require_all_confirm") and not cg.get("enabled"))

    loops = (status.get("loops") or {}).get("loops") or {}
    for name in ("heartbeat", "data_ingestion", "tradingview", "signal_generation", "verifier", "execution"):
        record(f"loop_{name}", name in loops)

    cfg = status.get("config") or {}
    record("config_grok_shadow", cfg.get("grok_decider_mode") == "shadow")
    _rr = float(cfg.get("min_reward_risk") or 0)
    record("config_reward_risk", 0.35 <= _rr <= 0.50)

    L = status.get("ledger") or {}
    trades = int(L.get("trades") or 0)
    ticks = int(status.get("ticks") or 0)
    record("ledger_reconciled", (status.get("decision_lifecycle") or {}).get("reconciled") is True)
    eg = status.get("execution_gate") or {}
    record("exec_gate_reconciled", eg.get("reconciled") is True)

    p = status.get("price") or {}
    record("price_feed", p.get("last_fetch_ok") is True and p.get("sampler_running") is True,
           f"age_s={p.get('age_s')}")
    rt = (status.get("oracle") or {}).get("rtds") or {}
    record("rtds_connected", rt.get("connected") is True)

    stop = status.get("stop_conditions") or {}
    dep_stop = (stop.get("strategies") or {}).get("dependency_arbitrage") or {}
    if dep_stop.get("halted"):
        issues.append(_issue(
            "dep_arb_halted", "P0",
            "reasons=%s metrics=%s" % (dep_stop.get("reasons"), dep_stop.get("metrics")),
            "set PULSE_STOP_DEP_ARB_GUARD_ENABLED=0 for paper soak or fix capture_ratio"))
    elif stop.get("any_halted"):
        issues.append(_issue("strategy_halted", "P0", str(stop.get("stalled") or stop),
                             "inspect stop_conditions"))

    dav = (status.get("dep_arb_intel") or {}).get("claude_verifier") or {}
    if dav.get("enabled") and dav.get("conjunction_only"):
        issues.append(_issue(
            "dep_arb_verifier_conjunction_only", "P1",
            "Claude dep-arb verifier skips nested_implication fills",
            "set PULSE_DEP_ARB_VERIFIER_CONJUNCTION_ONLY=0"))
    dep = status.get("dependency_arbitrage") or {}
    exp = dep.get("experiments") or {}
    # nested_implication is negative-EV raw, so it may execute ONLY behind an authoritative Claude
    # verifier (fail-closed + require-verdict). Flag the dangerous UNGATED config (nested on while the
    # verifier is fail-open / not require-verdict) — that is the -$406 bleed setup.
    _dav = (status.get("dep_arb_intel") or {}).get("claude_verifier") or {}
    _nested_on = exp.get("nested_execute_enabled") is True
    _gated = bool(_dav.get("enabled")) and bool(_dav.get("require_verdict")) and not _dav.get("fail_open")
    if _nested_on and not _gated:
        issues.append(_issue(
            "dep_arb_nested_ungated", "P1",
            "nested_execute on but Claude verifier not authoritative (fail_open=%s require_verdict=%s)"
            % (_dav.get("fail_open"), _dav.get("require_verdict")),
            "set PULSE_DEP_ARB_VERIFIER_FAIL_OPEN=0 + PULSE_DEP_ARB_VERIFIER_REQUIRE_VERDICT=1, "
            "or PULSE_DEPENDENCY_ARB_NESTED_EXECUTE=0"))
    rejects = dep.get("rejected_by_reason") or {}
    skew_rejects = sum(int(v) for k, v in rejects.items() if str(k).startswith("clock_skew_"))
    actionable = int(dep.get("actionable_detected") or 0)
    # Only flag starvation when the clock-skew filter is ACTUALLY enabled. When it is disabled the
    # clock_skew_* counts are stale lifetime totals from before it was turned off — they no longer
    # reject anything, so they must not raise a P0 (the old hint even said to disable an off filter).
    if exp.get("clock_skew_enabled") and actionable > 20 and skew_rejects >= actionable * 0.8:
        issues.append(_issue(
            "clock_skew_starving_fills", "P0",
            "actionable=%s clock_skew_rejects=%s rejects=%s" % (actionable, skew_rejects, rejects),
            "lower PULSE_DEPENDENCY_ARB_MIN_PARENT_BOOK_AGE_S or set PULSE_DEPENDENCY_ARB_CLOCK_SKEW_ENABLED=0"))
    elif actionable == 0 and int(dep.get("violations_detected") or 0) > 100:
        top = sorted(rejects.items(), key=lambda x: -int(x[1] or 0))[:3]
        issues.append(_issue(
            "dep_arb_no_actionable", "P2",
            "violations=%s actionable=0 top_rejects=%s" % (
                dep.get("violations_detected"), top),
            "check clock_skew, epsilon, bucket_bleeding, conjunction"))

    coupling = status.get("config_coupling") or {}
    if coupling.get("active"):
        record(
            "gate_coupling_ok",
            coupling.get("ok") is True,
            f"configured={coupling.get('configured_s')} effective={coupling.get('effective_s')} "
            f"required>={coupling.get('required_min_s')}",
        )
        if not coupling.get("configured_ok"):
            issues.append(_issue(
                "gate_coupling_misconfigured", "P0",
                f"PULSE_TV_CONTEXT_MAX_TTC_S={coupling.get('configured_s')} "
                f"need >={coupling.get('required_min_s')}",
                coupling.get("fix_hint") or "run apply-loop-arch-env.py",
            ))
        elif coupling.get("auto_clamped"):
            issues.append(_issue(
                "gate_coupling_clamped", "P2",
                f"runtime clamped context max to {coupling.get('effective_s')}",
                "fix .env so configured_s meets required_min_s",
            ))

    lc = status.get("decision_lifecycle") or {}
    rbs = lc.get("rejected_by_stage") or {}
    if int(rbs.get("verifier") or 0) > 100:
        issues.append(_issue("verifier_blocking", "P1", f"verifier_rejects={rbs.get('verifier')}",
                             "check verifier latency / fail_open"))

    sk = lc.get("skipped_by_reason") or {}
    recent = status.get("recent_evaluations") or []
    recent_reasons = [e.get("reason") or e.get("terminal_reason") for e in recent[-15:]]
    if ticks > 30 and recent_reasons and all(
            r in ("open_snapshot_late", "no_open_snapshot", "untrusted_vol") for r in recent_reasons if r):
        issues.append(_issue("window_skip_storm", "P1", f"recent={recent_reasons[:5]}",
                             "raise PULSE_MAX_OPEN_LAG_S or fix price sampler"))

    if ticks > 60 and trades <= 30 and int(rbs.get("directional") or 0) > 5000:
        issues.append(_issue("trade_freeze", "P1", f"trades={trades} ticks={ticks}",
                             "relax quant gates (min_edge, cohort, open_lag) — keep Grok shadow; "
                             "see soak-learning-lock.md"))

    metrics = {
        "trades": trades,
        "open_positions": L.get("open_positions"),
        "win_rate": L.get("win_rate"),
        "ticks": ticks,
        "grok_mode": gd.get("mode"),
        "verifier_enabled": ver.get("enabled"),
        "tv_valid": tv.get("tradingview_alerts_valid"),
        "status_age_s": round(age, 1),
        "top_gate_blockers": sorted(rbs.items(), key=lambda x: -x[1])[:5],
    }

    failed = [c for c in checks if not c["ok"]]
    healthy = len(issues) == 0 and len(failed) == 0
    verdict = "healthy" if healthy else ("blocked" if any(i["severity"] == "P0" for i in issues) else "issues")

    out = {
        "verdict": verdict,
        "healthy": healthy,
        "issues": issues,
        "checks": checks,
        "failed_checks": [c["name"] for c in failed],
        "metrics": metrics,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(out, indent=2))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())