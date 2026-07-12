#!/usr/bin/env python3
"""Validate VPS .env: secrets + frozen soak/learning manifest (run on VPS or via ssh)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Reuse frozen-lock validator
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from validate_frozen_lock import MANIFEST, VPS_ENV, validate_env, _parse_env, _issue  # noqa: E402


def main() -> int:
    env_path = Path(sys.argv[1]) if len(sys.argv) > 1 else VPS_ENV
    if not MANIFEST.exists():
        print(json.dumps({"healthy": False, "error": f"missing {MANIFEST}"}, indent=2))
        return 2

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    env = _parse_env(env_path)
    issues: list[dict] = []

    if not env:
        issues.append(_issue("env_missing", "P0", str(env_path)))

    issues.extend(validate_env(env, manifest))

    sev_order = {"P0": 0, "P1": 1, "P2": 2}
    issues.sort(key=lambda x: sev_order.get(x["severity"], 9))

    healthy = len(issues) == 0
    out = {
        "healthy": healthy,
        "verdict": "healthy" if healthy else ("blocked" if any(i["severity"] == "P0" for i in issues) else "issues"),
        "issues": issues,
        "env_path": str(env_path),
        "mode": manifest.get("mode"),
        "keys_present": len(env),
    }
    print(json.dumps(out, indent=2))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())