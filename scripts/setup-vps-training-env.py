#!/usr/bin/env python3
"""Prepare .env for Bot 3 VPS paper training (port 80 dashboard + TradingView webhooks)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine"
ENV_PATH = PLUGIN / ".env"
EXAMPLE = PLUGIN / ".env.example"
TV_SECRET_FILE = PLUGIN / "tradingview.secret"
TV_SECRET_EXAMPLE = PLUGIN / "tradingview.secret.example"
PROFILE_PATH = ROOT / "scripts" / "bot-profile.json"

# Local workspace runs setup-local-training-env.py; this profile is VPS-only.
VPS_OVERRIDES = {
    "PULSE_DASHBOARD_BOT_LABEL": "Bot 3 Directional",
    "PULSE_DASHBOARD_PUBLISH": "0.0.0.0:80",
    "TRADINGVIEW_WEBHOOK_PUBLISH": "127.0.0.1:18787",
    "TRADINGVIEW_WEBHOOK_HOST": "0.0.0.0",
    "TRADINGVIEW_WEBHOOK_UPSTREAM": "http://hermes-training:8787",
    "PULSE_TV_EVENT_ID_SUFFIX": "bot3",
    # TradingView: 5m RSI divergence only (INDEX BTC/ETH → 15m lane overlay).
    "PULSE_TV_MTF_TIMEFRAMES": "5",
    "PULSE_TV_RSI_BAND_ENABLED": "0",
    "PULSE_TV_15M_CHART_LEAN_ENABLED": "0",
    "PULSE_TV_1H_CHART_LEAN_ENABLED": "0",
    "PULSE_TV_2H_REVIEW_ENABLED": "0",
    "PULSE_TV_RSI_OVERLAY_ENABLED": "1",
    "PULSE_TV_RSI_DIVERGENCE_ANALYSIS_ENABLED": "1",
    "PULSE_BINARY_INTEL_ENABLED": "1",
    "PULSE_BINARY_INTEL_GROK_COMPUTE": "1",
    # Training throughput: paper learning — relax gates, CHRONOS observe-only.
    "PULSE_TRAINING_THROUGHPUT_MODE": "1",
    "PULSE_EXEC_TRAINING_MIN_EV": "-0.03",
    "PULSE_CHRONOS_ENABLED": "0",
    "PULSE_TRIAGE_TRAINING_SWEET_MIN": "0.20",
    "PULSE_TRIAGE_TRAINING_SWEET_MAX": "0.95",
    "PULSE_TRIAGE_TRAINING_MIN_DEPTH_USD": "5",
    "PULSE_TRIAGE_TRAINING_MIN_SHARES": "1",
    "PULSE_TRAINING_MIN_EDGE": "0",
    "PULSE_TRIAGE_FLAT_EXPLORATION_RATE": "0.95",
    "PULSE_TRIAGE_TREND_EXPLORATION_RATE": "0.95",
    "PULSE_TRIAGE_BTC_SWEET_MIN": "0.42",
    "PULSE_TRIAGE_BTC_SWEET_MAX": "0.78",
    "PULSE_TRIAGE_ETH_SWEET_MIN": "0.42",
    "PULSE_TRIAGE_ETH_SWEET_MAX": "0.78",
    "PULSE_TRIAGE_BTC_MIN_DEPTH_USD": "15",
    "PULSE_TRIAGE_ETH_MIN_DEPTH_USD": "15",
    "PULSE_TRIAGE_MIN_DEPTH_USD": "15",
    "PULSE_TRIAGE_TREND_SOURCE": "price",
    "PULSE_TRADINGVIEW_SIGNAL_GATE": "0",
    # Off during training — legacy tick path only; Osmani Discovery is price-trend authority.
    "PULSE_TV_CONTEXT_GATE": "0",
    "PULSE_TV_DOWN_BIAS_GATE": "0",
    "PULSE_TV_MTF_CONFLICT_GATE": "0",
    "PULSE_TV_CONFIDENCE_TIER_ENABLED": "1",
    "PULSE_TIER_QUANT_ONLY_WHEN_NO_TV": "1",
    "PULSE_DOWN_MAX_ASK_FAIR_GAP": "0.12",
}


def _parse_env(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _env_get(lines: list[str], key: str) -> str:
    for ln in lines:
        if ln.startswith(f"{key}="):
            raw = ln.split("=", 1)[1].strip()
            if raw.startswith('"') and raw.endswith('"'):
                return raw[1:-1].replace('\\"', '"')
            return raw
    return ""


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


def _load_tradingview_secret(lines: list[str]) -> str:
    if TV_SECRET_FILE.exists():
        for raw in TV_SECRET_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("TRADINGVIEW_WEBHOOK_SECRET="):
                return line.split("=", 1)[1].strip().strip('"')
            if line in ("PASTE_YOUR_SECRET_HERE", "CHANGE_ME", ""):
                continue
            return line
    return _env_get(lines, "TRADINGVIEW_WEBHOOK_SECRET")


def _ensure_secret_template() -> None:
    if TV_SECRET_FILE.exists() or not TV_SECRET_EXAMPLE.exists():
        return
    shutil.copy2(TV_SECRET_EXAMPLE, TV_SECRET_FILE)
    print(f"Created {TV_SECRET_FILE} - paste your TradingView secret before deploy")


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
    env_path = _resolve_env_path()
    plugin_dir = env_path.parent

    if not plugin_dir.is_dir():
        print(f"ERROR: plugin path missing: {plugin_dir}", file=sys.stderr)
        return 1

    _ensure_secret_template()

    if not env_path.exists():
        if not EXAMPLE.exists():
            print(f"ERROR: missing {EXAMPLE}", file=sys.stderr)
            return 1
        shutil.copy2(EXAMPLE, env_path)
        print(f"Created {env_path} from .env.example")

    apply = ROOT / "scripts" / "apply-loop-arch-env.py"
    if apply.exists():
        subprocess.run([sys.executable, str(apply)], check=True, cwd=ROOT)
    else:
        print(f"WARN: {apply} not found; using template .env only")

    lines = _parse_env(env_path)
    updates = dict(VPS_OVERRIDES)
    secret = _load_tradingview_secret(lines)
    if secret:
        updates["TRADINGVIEW_WEBHOOK_SECRET"] = secret
    lines = _upsert(lines, updates)
    if not any(ln.startswith("# BOT 3 VPS") for ln in lines):
        lines.append("# BOT 3 VPS - port 80 dashboard + TradingView webhooks")
    if not any(ln.startswith("# TRADINGVIEW VPS") for ln in lines):
        lines.append("# TRADINGVIEW VPS - secret from tradingview.secret on host")
    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote VPS training overrides to {env_path}")

    if secret:
        print("TradingView webhook: ENABLED (secret loaded)")
        print("  Alert URL : http://<vps-ip>/webhooks/tradingview")
        print("  Dashboard : http://<vps-ip>/dashboard  (label: Bot 3 Directional)")
        print("  Pine secret input: same value as tradingview.secret")
    else:
        print("TradingView webhook: DISABLED until secret is set")
        print(f"  Edit {TV_SECRET_FILE} on the VPS host, then re-run setup-vps-training-env.py")

    validate = ROOT / "scripts" / "pulse-babysit" / "validate-frozen-lock.py"
    if validate.exists():
        subprocess.run(
            [sys.executable, str(validate), str(env_path)],
            check=False,
            cwd=ROOT,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
