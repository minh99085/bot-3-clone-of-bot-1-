#!/usr/bin/env python3
"""Append a compact technical snapshot to monitoring/timeline.jsonl.

Run after pull-vps-artifacts or standalone (fetches live API).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LATEST = ROOT / "vps_full_reports" / "latest"
MONITOR = ROOT / "monitoring"
TIMELINE = MONITOR / "timeline.jsonl"
MANIFEST = MONITOR / "design-manifest.json"
DEFAULT_BASE = "http://144.202.122.120"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _fetch(url: str, timeout: int = 45) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _git_sha() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()[:12]
    except Exception:
        return None


def _config_fingerprint(status: dict) -> str:
    cfg = status.get("config") or {}
    cohort = status.get("baseline_cohort_gate") or {}
    tv = status.get("tradingview") or {}
    keys = {
        "tick": cfg.get("tick_seconds"),
        "max_price": cfg.get("max_price"),
        "min_edge": cfg.get("min_edge"),
        "min_rr": cfg.get("min_reward_risk"),
        "grok": cfg.get("grok_decider_mode"),
        "series": status.get("pulse_series_slugs"),
        "down_only": status.get("directional_down_only"),
        "green_path": cohort.get("green_path_enabled"),
        "cohort_hi_edge": cohort.get("require_high_edge"),
        "cohort_strong_cex": cohort.get("require_strong_cex"),
        "ttc_band": cohort.get("15m_ttc_band_s"),
        "tv_signal_gate": (tv.get("signal_gate") or {}).get("enabled"),
        "tv_mtf_gate": (tv.get("mtf_gate") or {}).get("enabled"),
    }
    blob = json.dumps(keys, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _top_rejections(lc: dict, n: int = 6) -> dict:
    rbs = lc.get("rejected_by_stage") or {}
    return dict(sorted(rbs.items(), key=lambda x: -x[1])[:n])


def _recent_eval_counts(status: dict) -> dict:
    ev = status.get("recent_evaluations") or []
    return dict(Counter(r.get("terminal_reason") or "unknown" for r in ev).most_common())


def _tv_snapshot(tv: dict) -> dict:
    mtf = tv.get("tradingview_mtf_confirmation") or {}
    by_tf = tv.get("tradingview_latest_by_timeframe") or {}
    sym = tv.get("tradingview_feature_symbol") or "BTCUSD"
    tfs = {}
    for tf in ("2", "3", "4"):
        snap = by_tf.get(f"{sym}@{tf}") or by_tf.get(f"INDEX:{sym}@{tf}") or {}
        tfs[f"{tf}m"] = {
            "dir": snap.get("direction"),
            "strength": snap.get("strength"),
            "age_s": mtf.get(f"tf_{tf}m_age_s"),
        }
    return {
        "alerts_valid": tv.get("tradingview_alerts_valid"),
        "alerts_rejected": tv.get("tradingview_alerts_rejected"),
        "mtf_verdict": mtf.get("confirm_mtf") or mtf.get("confirm_3tf"),
        "trend_fresh": mtf.get("trend_fresh_count"),
        "mtf_count": mtf.get("mtf_count"),
        "signal_gate": (tv.get("signal_gate") or {}).get("enabled"),
        "mtf_gate": (tv.get("mtf_gate") or {}).get("enabled"),
        "by_tf": tfs,
    }


def build_record(status: dict, *, source: str, repo_sha: str | None) -> dict:
    cfg = status.get("config") or {}
    cohort = status.get("baseline_cohort_gate") or {}
    lc = status.get("decision_lifecycle") or {}
    cap = status.get("capital") or {}
    L = status.get("ledger") or {}
    eg = status.get("execution_gate") or {}
    gd = status.get("grok_decider") or {}
    ver = status.get("verifier") or {}
    price = status.get("price") or {}
    oracle = status.get("oracle") or {}
    rtds = (oracle.get("rtds") or {})
    band = cohort.get("15m_ttc_band_s") or [160, 220]
    scale = 3.0

    now = datetime.now(timezone.utc)
    fp = _config_fingerprint(status)

    return {
        "schema": "timeline/1.0",
        "ts_utc": now.isoformat(),
        "source": source,
        "repo_sha": repo_sha,
        "config_fingerprint": fp,
        "runtime": {
            "ticks": status.get("ticks"),
            "status_age_s": None,
            "paper_only": status.get("paper_only"),
            "halted": (status.get("stop_conditions") or {})
            .get("strategies", {})
            .get("directional", {})
            .get("halted"),
            "reconciled": (status.get("reconciliation") or {}).get("global_reconciled"),
        },
        "design": {
            "series": status.get("pulse_series_slugs"),
            "down_only": status.get("directional_down_only"),
            "green_path": cohort.get("green_path_enabled"),
            "ttc_band_15m_s": [band[0] * scale, band[1] * scale],
            "tick_s": cfg.get("tick_seconds"),
            "max_price": cfg.get("max_price"),
            "min_edge": cfg.get("min_edge"),
            "min_rr": cfg.get("min_reward_risk"),
            "grok_mode": cfg.get("grok_decider_mode"),
            "require_high_edge": cohort.get("require_high_edge"),
            "require_strong_cex": cohort.get("require_strong_cex"),
        },
        "tv": _tv_snapshot(status.get("tradingview") or {}),
        "oracle": {
            "btc_usd": price.get("last_price"),
            "source": price.get("source"),
            "rtds_connected": rtds.get("connected"),
            "oracle_age_s": rtds.get("oracle_age_s"),
        },
        "ledger": {
            "trades": L.get("trades"),
            "settled": L.get("settled"),
            "win_rate": L.get("win_rate"),
            "profit_factor": L.get("profit_factor"),
            "pnl_usd": cap.get("realized_pnl_usd"),
            "on_hand_usd": cap.get("on_hand_capital_usd"),
            "open_positions": L.get("open_positions"),
        },
        "funnel": {
            "top_rejects": _top_rejections(lc),
            "cohort_session_blocks": cohort.get("blocked"),
            "cohort_block_reasons": cohort.get("block_reasons"),
            "exec_candidates": eg.get("candidates"),
            "exec_accepted": eg.get("accepted"),
            "exec_rejected": eg.get("rejected_total"),
        },
        "grok_verifier": {
            "decided": gd.get("decided"),
            "abstains": gd.get("abstains"),
            "errors": gd.get("errors"),
            "verifier_approvals": ver.get("approvals"),
            "verifier_vetoes": ver.get("vetoes"),
        },
        "recent_evals": _recent_eval_counts(status),
        "by_series": {
            k: {
                "settled": v.get("settled"),
                "win_rate": v.get("win_rate"),
                "pnl_usd": v.get("pnl_usd"),
            }
            for k, v in (status.get("by_market_series") or {}).items()
        },
    }


def _last_fingerprint() -> str | None:
    if not TIMELINE.exists():
        return None
    try:
        last = TIMELINE.read_text(encoding="utf-8").strip().splitlines()[-1]
        return json.loads(last).get("config_fingerprint")
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Record pulse bot timeline snapshot")
    ap.add_argument("--base", default=DEFAULT_BASE, help="VPS API base URL")
    ap.add_argument("--from-latest", action="store_true", help="Use vps_full_reports/latest instead of API")
    ap.add_argument("--archive", action="store_true", help="Copy latest artifacts to monitoring/archives/<ts>/")
    args = ap.parse_args()

    MONITOR.mkdir(parents=True, exist_ok=True)

    if args.from_latest and (LATEST / "btc_pulse_status.json").exists():
        status = _load_json(LATEST / "btc_pulse_status.json")
        source = "local_latest"
    else:
        try:
            status = _fetch(f"{args.base.rstrip('/')}/api/polymarket/training/btc_pulse")
            source = "api"
        except Exception as exc:
            print(f"API fetch failed: {exc}", file=sys.stderr)
            if (LATEST / "btc_pulse_status.json").exists():
                status = _load_json(LATEST / "btc_pulse_status.json")
                source = "local_fallback"
            else:
                return 1

    if not status or status.get("available") is False:
        print("No pulse status available", file=sys.stderr)
        return 1

    repo_sha = _git_sha()
    rec = build_record(status, source=source, repo_sha=repo_sha)
    prev_fp = _last_fingerprint()
    rec["config_changed"] = bool(prev_fp and prev_fp != rec["config_fingerprint"])

    with TIMELINE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")

    (MONITOR / "latest-snapshot.json").write_text(
        json.dumps(rec, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    if args.archive and (LATEST / "btc_pulse_status.json").exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = MONITOR / "archives" / stamp
        dest.mkdir(parents=True, exist_ok=True)
        for name in (
            "btc_pulse_status.json",
            "btc_pulse_ledger.json",
            "btc_pulse_light_report.json",
            "CYCLE_SUMMARY.md",
        ):
            src = LATEST / name
            if src.exists():
                dest.joinpath(name).write_bytes(src.read_bytes())

    changed = " [CONFIG CHANGED]" if rec["config_changed"] else ""
    print(
        f"timeline +1{changed} trades={rec['ledger']['trades']} "
        f"wr={rec['ledger']['win_rate']} ticks={rec['runtime']['ticks']} "
        f"-> {TIMELINE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())