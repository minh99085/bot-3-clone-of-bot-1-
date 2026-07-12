#!/usr/bin/env python3
"""Prepare .env for local Bot 3 paper training (Docker Desktop)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine"
ENV_PATH = PLUGIN / ".env"
EXAMPLE = PLUGIN / ".env.example"

LOCAL_OVERRIDES = {
    "PULSE_DASHBOARD_BOT_LABEL": "Bot 3 - Local Training",
    # Host port 8810 avoids collision with Bot-1 on 8800 (see docker-compose.local.yml).
    "PULSE_DASHBOARD_PUBLISH": "127.0.0.1:8810",
    "TRADINGVIEW_WEBHOOK_PUBLISH": "127.0.0.1:18787",
    "PAPER_TRAINING_ENABLED": "1",
    "POLYMARKET_PAPER_TRAINING_ENABLED": "1",
    "BTC_PULSE_ENABLED": "1",
    "BTC_PULSE_PAPER_ONLY": "1",
    "LIVE_TRADING_ENABLED": "0",
    "POLYMARKET_LIVE_ENABLED": "0",
    # Local no-key profile: quant loop trains without Grok/TV webhook (add keys in .env later).
    "GROK_SIGNAL_ANALYST_ENABLED": "0",
    "GROK_SIGNAL_PREDICTOR_ENABLED": "0",
    "GROK_OVERLAY_ENABLED": "0",
    "PULSE_GROK_DECIDER_MODE": "shadow",
    "PULSE_RESEARCH_LOOP_ENABLED": "0",
    "PULSE_VERIFIER_ENABLED": "0",
}


def _parse_env(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _format_env_value(val: str) -> str:
    """Quote .env values that would break docker compose parsing on Windows."""
    if any(c in val for c in " \t#\"'") or not val:
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return val


def _upsert(lines: list[str], updates: dict[str, str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        if "=" in ln and not ln.lstrip().startswith("#"):
            key = ln.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={_format_env_value(updates[key])}")
                seen.add(key)
                continue
        if ln.strip() or out:
            out.append(ln)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={_format_env_value(val)}")
    return out


def main() -> int:
    if not PLUGIN.is_dir():
        print(f"ERROR: plugin path missing: {PLUGIN}", file=sys.stderr)
        return 1

    if not ENV_PATH.exists():
        if not EXAMPLE.exists():
            print(f"ERROR: missing {EXAMPLE}", file=sys.stderr)
            return 1
        shutil.copy2(EXAMPLE, ENV_PATH)
        print(f"Created {ENV_PATH} from .env.example")

    apply = ROOT / "scripts" / "apply-loop-arch-env.py"
    if apply.exists():
        subprocess.run([sys.executable, str(apply)], check=True, cwd=ROOT)
    else:
        print(f"WARN: {apply} not found; using template .env only")

    lines = _parse_env(ENV_PATH)
    lines = _upsert(lines, LOCAL_OVERRIDES)
    if not any(ln.startswith("# LOCAL BOT 3") for ln in lines):
        lines.append("# LOCAL BOT 3 - laptop Docker training profile")
    ENV_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote local training overrides to {ENV_PATH}")

    validate = ROOT / "scripts" / "pulse-babysit" / "validate-frozen-lock.py"
    if validate.exists():
        subprocess.run([sys.executable, str(validate)], check=False, cwd=ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
