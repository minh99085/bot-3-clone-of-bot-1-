"""Local .env loader (deployment-agnostic).

docker-compose only auto-loads a file named exactly ``.env`` from the compose
directory for ``${VAR}`` substitution; a file named ``.env.env`` (a common
save-as mistake) is silently ignored, so keys like ``XAI_API_KEY`` never reach
the process and the Grok research layer reports "no API key". This loader makes
both docker and local runs read the key (+ paper config) from ``.env`` and, as a
fallback, ``.env.env`` — WITHOUT enabling live trading.

Safety: live/autotrade flags can NEVER be turned on via a dotenv file here — they
are force-pinned OFF regardless of file content (paper-only build). Existing
non-empty process env always wins; only absent/empty vars are filled in.
"""

from __future__ import annotations

import os
from pathlib import Path

# These flags are NEVER loaded from a dotenv file — a dotenv file can neither
# enable a live/real-money path nor MASK an operator-set live flag (so the startup
# preflight can still detect + refuse it). Defense-in-depth atop the paper locks.
_FORBIDDEN_LIVE_FLAGS = {
    "LIVE_TRADING_ENABLED", "POLYMARKET_LIVE_ENABLED", "POLYMARKET_LIVE_TRADING_ENABLED",
    "POLYMARKET_AUTOTRADE_ENABLED", "BTC_AUTOTRADE_ENABLED", "BTC_PULSE_LIVE_ENABLED",
    "GUARDED_LIVE_ENABLED", "MICRO_LIVE_ENABLED", "KALSHI_MICRO_LIVE_ENABLED",
    "POLYMARKET_MICRO_LIVE_ENABLED", "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION",
    "PRODUCTION_REVIEW_ALLOW_AUTONOMOUS_LIVE", "ARB_EXECUTION_ENABLED",
    "MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK", "MICRO_LIVE_ALLOW_PRODUCTION",
}

# Candidate filenames in priority order (correct name first, then the typo).
_ENV_FILENAMES = (".env", ".env.env", ".env.local")


def _plugin_root() -> Path:
    # engine/env_loader.py -> plugin root is one level up from engine/
    return Path(__file__).resolve().parent.parent


def _parse(text: str) -> dict:
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):]
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load_local_env(*, root: "Path | str | None" = None, override: bool = False) -> dict:
    """Load .env/.env.env into ``os.environ``. Returns {var: source_file}.

    A var is set only when absent or empty in the current environment (unless
    ``override``). Forbidden live flags are always pinned to "0"."""
    base = Path(root) if root else _plugin_root()
    applied: dict = {}
    for name in _ENV_FILENAMES:
        p = base / name
        if not p.exists() or not p.is_file():
            continue
        try:
            parsed = _parse(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a bad env file must never crash startup
            continue
        for k, v in parsed.items():
            if k in _FORBIDDEN_LIVE_FLAGS:
                # NEVER load a live/autotrade flag from a dotenv file — and never
                # mask an operator-set one either (so the startup preflight can
                # still detect + refuse a real live flag). Just skip it.
                applied[k] = f"{name}(skipped_live_flag)"
                continue
            cur = os.environ.get(k, "")
            if override or not str(cur).strip():
                os.environ[k] = v
                applied.setdefault(k, name)
    return applied


def grok_key_present() -> bool:
    """True when an xAI/Grok API key is available in the process environment."""
    return bool((os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip())


# Research-only online modes (xAI research calls; NEVER a trading/live path). Mirror
# of engine.research.schemas.ONLINE_MODES (kept local to avoid an import cycle).
_RESEARCH_ONLINE_MODES = ("online_paper", "online_shadow", "guarded_live_readonly", "online")


def enable_grok_research_if_key_present(default_mode: str = "online_paper") -> str:
    """Turn the xAI/Grok research layer ON when a key is present but RESEARCH_MODE is
    unset/empty: default it to ``online_paper`` (research-only; NEVER live trading).

    So "the key is in .env" is sufficient to activate xAI without also having to set
    RESEARCH_MODE by hand. If RESEARCH_MODE is already set, it is respected. Returns
    the resolved RESEARCH_MODE (or "" when no key is present). Paper-only."""
    if not grok_key_present():
        return os.environ.get("RESEARCH_MODE", "")
    cur = (os.environ.get("RESEARCH_MODE") or "").strip()
    if cur:
        return cur
    os.environ["RESEARCH_MODE"] = default_mode
    return default_mode
