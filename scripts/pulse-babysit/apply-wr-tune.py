#!/usr/bin/env python3
"""Deterministic WR tune: read 24h price bands, patch apply-loop-arch-env + frozen manifest."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BABYSIT = Path(__file__).resolve().parent
LEDGER = ROOT / "vps_full_reports" / "latest" / "btc_pulse_ledger.json"
POLICY_PATH = BABYSIT / "wr-tune-policy.json"
FROZEN_PATH = BABYSIT / "frozen-env-keys.json"
ENV_SCRIPT = ROOT / "scripts" / "apply-loop-arch-env.py"
STATE_PATH = BABYSIT / "state.json"

sys.path.insert(0, str(BABYSIT))
from price_band_analysis import analyze_price_bands, detect_band_issues  # noqa: E402


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_env_key(script_text: str, key: str, default: float) -> float:
    m = re.search(rf'"{re.escape(key)}":\s*"([0-9.]+)"', script_text)
    return float(m.group(1)) if m else default


def _patch_key_in_file(path: Path, key: str, value: str) -> bool:
    text = path.read_text(encoding="utf-8")
    pattern = rf'("{re.escape(key)}":\s*")[^"]+(")'
    new_text, n = re.subn(pattern, rf"\g<1>{value}\g<2>", text, count=1)
    if n == 0:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def _starvation_active(eval_issues: list[dict] | None, policy: dict) -> bool:
    skip = set(policy.get("starvation_guard", {}).get("skip_wr_tune_issue_codes") or [])
    for iss in eval_issues or []:
        if iss.get("code") in skip:
            return True
    return False


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _plan_fixes(
    analysis: dict,
    issues: list[dict],
    policy: dict,
    *,
    min_entry: float,
    max_price: float,
) -> list[dict]:
    """Return ordered fix actions (max 2 per policy)."""
    triggers = policy.get("triggers") or {}
    priority = policy.get("priority") or []
    guard = policy.get("starvation_guard") or {}
    floor_min = float(guard.get("never_lower_min_entry_price_below", 0.45))
    max_floor = float(policy.get("max_price_floor", 0.58))
    max_ceil = float(policy.get("max_price_ceiling", 0.75))
    max_fixes = int(policy.get("max_fixes_per_cycle", 2))

    issue_codes = {i["code"] for i in issues}
    fixes: list[dict] = []

    for code in priority:
        if code not in issue_codes:
            continue
        trig = triggers.get(code) or {}
        if code == "cheap_down_bleed":
            step = float(trig.get("step", 0.01))
            cap = float(trig.get("cap", 0.48))
            new_val = _clamp(min_entry + step, floor_min, cap)
            if new_val > min_entry + 1e-9:
                fixes.append({
                    "issue": code,
                    "key": "PULSE_MIN_ENTRY_PRICE",
                    "from": min_entry,
                    "to": round(new_val, 2),
                    "reason": "cheap DOWN band bleeding WR",
                })
                min_entry = new_val
        elif code == "expensive_down_bleed":
            step = float(trig.get("step", 0.02))
            floor = float(trig.get("floor", max_floor))
            new_val = _clamp(max_price - step, floor, max_ceil)
            if new_val < max_price - 1e-9:
                fixes.append({
                    "issue": code,
                    "key": "PULSE_MAX_PRICE",
                    "from": max_price,
                    "to": round(new_val, 2),
                    "reason": "expensive DOWN band bleeding WR/PnL",
                })
                max_price = new_val
        elif code == "sweet_spot_underuse":
            tgt_min = float(trig.get("min_entry_target", 0.45))
            tgt_max = float(trig.get("max_price_target", 0.55))
            if min_entry > tgt_min + 1e-9:
                fixes.append({
                    "issue": code,
                    "key": "PULSE_MIN_ENTRY_PRICE",
                    "from": min_entry,
                    "to": tgt_min,
                    "reason": "sweet spot high WR but underused — tighten floor",
                })
                min_entry = tgt_min
            elif max_price > tgt_max + 1e-9:
                fixes.append({
                    "issue": code,
                    "key": "PULSE_MAX_PRICE",
                    "from": max_price,
                    "to": tgt_max,
                    "reason": "sweet spot high WR but underused — tighten ceiling",
                })
                max_price = tgt_max

        if len(fixes) >= max_fixes:
            break

    return fixes


def apply_fixes(fixes: list[dict], *, dry_run: bool) -> list[dict]:
    applied: list[dict] = []
    for fix in fixes:
        key = fix["key"]
        val = f"{fix['to']:.2f}".rstrip("0").rstrip(".")
        if "." not in val:
            val = f"{fix['to']:.2f}"
        entry = {**fix, "value": val, "applied": False}
        if not dry_run:
            ok_env = _patch_key_in_file(ENV_SCRIPT, key, val)
            ok_frozen = _patch_key_in_file(
                FROZEN_PATH, key, val,
            ) if FROZEN_PATH.exists() else False
            # frozen manifest nests keys under learning_collection_frozen
            if not ok_frozen:
                text = FROZEN_PATH.read_text(encoding="utf-8")
                pattern = rf'("{re.escape(key)}":\s*")[^"]+(")'
                new_text, n = re.subn(pattern, rf"\g<1>{val}\g<2>", text, count=1)
                if n:
                    FROZEN_PATH.write_text(new_text, encoding="utf-8")
                    ok_frozen = True
            entry["applied"] = ok_env and ok_frozen
        applied.append(entry)
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply deterministic WR price-band tune")
    parser.add_argument("--apply", action="store_true", help="Write patches to repo files")
    parser.add_argument("--dry-run", action="store_true", help="Plan only (default)")
    parser.add_argument("--eval-json", type=str, default="", help="evaluate-cycle JSON for starvation guard")
    args = parser.parse_args()
    dry_run = not args.apply

    policy = _load(POLICY_PATH)
    ledger = _load(LEDGER)
    state = _load(STATE_PATH)
    eval_data = json.loads(args.eval_json) if args.eval_json else {}

    lookback = float(policy.get("lookback_hours", 24))
    side = policy.get("side", "down")
    engine_ts = None
    if eval_data.get("metrics", {}).get("ticks"):
        pass
    analysis = analyze_price_bands(
        ledger,
        lookback_hours=lookback,
        side=side,
        policy=policy,
    )
    band_issues = detect_band_issues(analysis, policy)
    eval_issues = eval_data.get("issues") or []

    env_text = ENV_SCRIPT.read_text(encoding="utf-8") if ENV_SCRIPT.exists() else ""
    frozen = _load(FROZEN_PATH)
    frozen_vals = frozen.get("learning_collection_frozen") or {}
    min_entry = _read_env_key(
        env_text, "PULSE_MIN_ENTRY_PRICE",
        float(frozen_vals.get("PULSE_MIN_ENTRY_PRICE", 0.45)),
    )
    max_price = _read_env_key(
        env_text, "PULSE_MAX_PRICE",
        float(frozen_vals.get("PULSE_MAX_PRICE", 0.62)),
    )

    skipped_reason = None
    fixes: list[dict] = []
    frozen_auth = frozen.get("authority_frozen") or {}
    directional_on = frozen_auth.get("PULSE_DIRECTIONAL_ENABLED", "1") == "1"
    if not directional_on and policy.get("starvation_guard", {}).get(
        "skip_when_directional_disabled", True
    ):
        skipped_reason = "directional_disabled"
    elif _starvation_active(eval_issues, policy):
        skipped_reason = "starvation_active"
    elif (state.get("goals") or {}).get("mode") != "real_money_discipline":
        skipped_reason = "not_real_money_discipline"
    elif not band_issues:
        skipped_reason = "no_band_issues"
    else:
        fixes = _plan_fixes(
            analysis, band_issues, policy,
            min_entry=min_entry, max_price=max_price,
        )
        if not fixes:
            skipped_reason = "no_actionable_fixes"

    applied = apply_fixes(fixes, dry_run=dry_run) if fixes else []

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": (state.get("goals") or {}).get("mode"),
        "dry_run": dry_run,
        "skipped_reason": skipped_reason,
        "price_band_24h": analysis,
        "band_issues": band_issues,
        "planned_fixes": fixes,
        "applied_fixes": applied,
        "current": {
            "PULSE_MIN_ENTRY_PRICE": min_entry,
            "PULSE_MAX_PRICE": max_price,
        },
    }
    print(json.dumps(out, indent=2))
    if applied and not dry_run and not all(a.get("applied") for a in applied):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())