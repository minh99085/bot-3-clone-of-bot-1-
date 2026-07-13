#!/usr/bin/env python3
"""VPS Profile B: favorites WR policy from 30d offline replay (paper A/B).

Replaces training-throughput wide gates with favorites floor + cell Phase-2
on Osmani path. Tag fills with PULSE_AB_PROFILE=favorites for metrics.

Usage:
  python3 scripts/setup-vps-favorites-ab-env.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine"
ENV_PATH = PLUGIN / ".env"
TV_SECRET_FILE = PLUGIN / "tradingview.secret"
PROFILE_PATH = ROOT / "scripts" / "bot-profile.json"

# Profile B — favorites book (offline holdout ~73% WR at ask>=0.48).
FAVORITES_OVERRIDES = {
    "PULSE_DASHBOARD_BOT_LABEL": "Bot 3 Favorites A/B",
    "PULSE_AB_PROFILE": "favorites",
    "PULSE_FAVORITES_POLICY": "1",
    # Turn OFF throughput wide-band (Profile A contrast).
    "PULSE_TRAINING_THROUGHPUT_MODE": "0",
    "PULSE_EXEC_TRAINING_MIN_EV": "0",
    "PULSE_TRAINING_MIN_EDGE": "0.003",
    # Favorites floor tightened from offline holdout: ask>=0.48 alone ≈ coin-flip
    # live (51.6%); mid-band favorites (ask≈0.60+) showed ~63–66% WR with +EV.
    # Tail-high (~0.82) hits 84–86% WR but thin EV — floor 0.58 targets mid+.
    "PULSE_MIN_ENTRY_PRICE": "0.58",
    "PULSE_TRIAGE_BTC_SWEET_MIN": "0.58",
    "PULSE_TRIAGE_BTC_SWEET_MAX": "0.78",
    "PULSE_TRIAGE_ETH_SWEET_MIN": "0.58",
    "PULSE_TRIAGE_ETH_SWEET_MAX": "0.78",
    "PULSE_TIER_SWEET_MIN": "0.58",
    "PULSE_TIER_SWEET_MAX": "0.78",
    # Cell learning Phase-2 on tier + Osmani (offline warm-start in /data).
    "PULSE_CELL_LEARNING_ENABLED": "1",
    "PULSE_CELL_LEARNING_PHASE2_ENABLED": "1",
    "PULSE_CELL_LEARNING_MIN_SAMPLES": "8",
    "PULSE_CELL_PHASE2_BLOCK_FADE": "1",
    "PULSE_LANE_OFFLINE_PRIOR": "1",
    "PULSE_LANE_15M_LEARN_ENABLED": "1",
    # CHRONOS active (training mode had it off).
    "PULSE_CHRONOS_ENABLED": "1",
    "PULSE_CHRONOS_EXPLORATION_RATE": "0.10",
    # Moderate discovery — not starvation, not firehose.
    "PULSE_TRIAGE_FLAT_EXPLORATION_RATE": "0.30",
    "PULSE_TRIAGE_TREND_EXPLORATION_RATE": "0.20",
    "PULSE_EXEC_MIN_EV": "0",
    "PULSE_EXEC_MAX_SPREAD": "0.08",
    "PULSE_SAWR_ENABLED": "0",
    "PULSE_GATE_AUTO_TUNE_ENABLED": "0",
    "PULSE_TRIAGE_MIN_DEPTH_USD": "15",
    "PULSE_TRIAGE_BTC_MIN_DEPTH_USD": "15",
    "PULSE_TRIAGE_ETH_MIN_DEPTH_USD": "15",
    "PULSE_TRIAGE_TREND_SOURCE": "price",
    "PULSE_AB_STARTED_AT": "auto",
}


def _parse_env(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _format_env_value(val: str) -> str:
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


def _resolve_env_path() -> Path:
    if PROFILE_PATH.exists():
        import json
        try:
            prof = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            vps_repo = (prof.get("vps_repo") or "").strip()
            if vps_repo:
                candidate = Path(vps_repo) / "hermes-agent-main/plugins/hermes-trading-engine/.env"
                if candidate.exists() or Path(vps_repo).exists():
                    return candidate
        except (json.JSONDecodeError, OSError):
            pass
    for candidate in (
        Path("/opt/Bot-3/hermes-agent-main/plugins/hermes-trading-engine/.env"),
        ENV_PATH,
    ):
        if candidate.exists():
            return candidate
    return ENV_PATH


def main() -> int:
    from datetime import datetime, timezone

    env_path = _resolve_env_path()
    if not env_path.parent.is_dir():
        print(f"ERROR: missing plugin dir {env_path.parent}", file=sys.stderr)
        return 1

    apply = ROOT / "scripts" / "apply-loop-arch-env.py"
    if apply.exists():
        subprocess.run([sys.executable, str(apply)], check=True, cwd=ROOT)

    lines = _parse_env(env_path)
    updates = dict(FAVORITES_OVERRIDES)
    updates["PULSE_AB_STARTED_AT"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Preserve TV secret from training setup
    setup_train = ROOT / "scripts" / "setup-vps-training-env.py"
    if setup_train.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("vps_train", setup_train)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        secret = mod._load_tradingview_secret(lines)
        if secret:
            updates["TRADINGVIEW_WEBHOOK_SECRET"] = secret

    lines = _upsert(lines, updates)
    if not any("FAVORITES A/B" in ln for ln in lines):
        lines.append("# BOT 3 VPS FAVORITES A/B — Profile B (30d offline replay)")
    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote favorites A/B profile to {env_path}")
    print("  PULSE_AB_PROFILE=favorites")
    print("  PULSE_MIN_ENTRY_PRICE=%s" % updates.get("PULSE_MIN_ENTRY_PRICE", "0.58"))
    print("  PULSE_CELL_LEARNING_PHASE2_ENABLED=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
