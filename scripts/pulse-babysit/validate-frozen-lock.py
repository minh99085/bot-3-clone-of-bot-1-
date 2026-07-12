#!/usr/bin/env python3
"""Validate .env against the retained-invariants manifest. Prints JSON; exit 1 on drift.

Soak/learning freeze removed 2026-07-06 — the manifest freeze lists are empty; this now enforces
only required secrets + the retained invariants (PAPER ONLY, honest accounting, reconciliation)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).resolve().parent / "frozen-env-keys.json"
DEFAULT_ENV = ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine" / ".env"
VPS_ENV = Path("/opt/Bot-1/hermes-agent-main/plugins/hermes-trading-engine/.env")


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _float_val(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _issue(code: str, severity: str, detail: str, hint: str = "") -> dict:
    return {"code": code, "severity": severity, "detail": detail, "hint": hint}


def validate_env(env: dict[str, str], manifest: dict) -> list[dict]:
    issues: list[dict] = []

    for key in manifest.get("required_nonempty") or []:
        if not env.get(key):
            issues.append(_issue(
                "secret_missing", "P0", key,
                f"set {key} in .env and recreate hermes-training",
            ))

    for section in ("authority_frozen", "learning_collection_frozen"):
        for key, want in (manifest.get(section) or {}).items():
            got = env.get(key)
            if got != want:
                sev = "P0" if section == "authority_frozen" else "P1"
                issues.append(_issue(
                    "frozen_drift", sev, f"{key}={got!r} want={want!r}",
                    "run scripts/apply-loop-arch-env.py to re-sync .env with the manifest",
                ))

    for key in manifest.get("forbidden_enable") or []:
        if env.get(key) == "1":
            issues.append(_issue(
                "forbidden_gate_enabled", "P0", f"{key}=1",
                "TV/authority gate re-enabled — see tv-observe-only-lock.md",
            ))

    for key in manifest.get("forbidden_tighten_to_one") or []:
        frozen_val = (manifest.get("learning_collection_frozen") or {}).get(key, "0")
        got = env.get(key)
        if frozen_val == "0" and got == "1" and manifest.get("mode") != "real_money_discipline":
            issues.append(_issue(
                "forbidden_tighten", "P1", f"{key}=1 (frozen=0)",
                "do not re-tighten during learning_collection — relax quant gates only",
            ))

    for key, bounds in (manifest.get("tunable_bounds") or {}).items():
        frozen_val = (manifest.get("learning_collection_frozen") or {}).get(key)
        got_f = _float_val(env.get(key))
        frozen_f = _float_val(frozen_val)
        if got_f is None or frozen_f is None:
            continue
        direction = bounds.get("direction")
        lo = float(bounds["min"])
        hi = float(bounds["max"])
        if got_f < lo or got_f > hi:
            issues.append(_issue(
                "tunable_out_of_bounds", "P2",
                f"{key}={got_f} outside [{lo}, {hi}]",
                "stay within tunable_bounds in frozen-env-keys.json",
            ))
        elif direction == "relax_only" and got_f > frozen_f:
            if manifest.get("mode") != "real_money_discipline":
                issues.append(_issue(
                    "forbidden_tighten", "P1",
                    f"{key}={got_f} tightened above frozen {frozen_f}",
                    "learning_collection allows relax-only on this key",
                ))
        elif direction == "raise_only" and got_f < frozen_f:
            issues.append(_issue(
                "forbidden_relax", "P2",
                f"{key}={got_f} below frozen floor {frozen_f}",
                "only raise this key when strategy_halted blocks trading",
            ))

    return issues


def main() -> int:
    env_path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        VPS_ENV if VPS_ENV.exists() else DEFAULT_ENV
    )
    if not MANIFEST.exists():
        print(json.dumps({"healthy": False, "error": f"missing {MANIFEST}"}, indent=2))
        return 2

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    env = _parse_env(env_path)
    issues: list[dict] = []

    if not env:
        issues.append(_issue("env_missing", "P0", str(env_path), "create .env on target host"))

    issues.extend(validate_env(env, manifest))

    sev_order = {"P0": 0, "P1": 1, "P2": 2}
    issues.sort(key=lambda x: sev_order.get(x["severity"], 9))

    healthy = len(issues) == 0
    out = {
        "healthy": healthy,
        "verdict": "healthy" if healthy else ("blocked" if any(i["severity"] == "P0" for i in issues) else "issues"),
        "mode": manifest.get("mode"),
        "env_path": str(env_path),
        "issues": issues,
        "frozen_keys_checked": len(manifest.get("authority_frozen") or {})
                                + len(manifest.get("learning_collection_frozen") or {}),
    }
    print(json.dumps(out, indent=2))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())