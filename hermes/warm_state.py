"""B2 — kill cold-start: persist rolling price history + σ-ratio EWMA to disk.

Every redeploy used to reset the in-memory CEX tick history (→ no realized σ,
no momentum, barrier q degraded to priors for the first ~10 minutes) and the
implied/realized σ EWMA (→ market_sigma lanes re-learn from scratch). Both are
tiny JSON snapshots per instance now: saved continuously (throttled), warm-
loaded on startup with a staleness guard so a long outage never resurrects
ancient state.

All I/O is best-effort — a broken/corrupt/missing warm file must NEVER break
the trading loop; it just means a cold start like before.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Ticks older than the in-memory rolling window are useless to restore.
HISTORY_MAX_AGE_SEC = 600.0
# σ-ratio EWMA drifts slowly (α=0.05); accept up to 6h old on restart.
SIGMA_MAX_AGE_SEC = 6 * 3600.0


def warm_dir() -> Path:
    override = os.environ.get("HERMES_WARM_DIR", "").strip()
    if override:
        return Path(override)
    from hermes.state_io import DATA, _instance_id

    return DATA / "warm" / _instance_id()


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ── Price history ────────────────────────────────────────────────────────────

def save_price_history(hist: dict[str, list[tuple[float, float]]]) -> None:
    """Snapshot {asset: [(epoch, price), ...]} — call throttled from the feed."""
    try:
        _atomic_write_json(
            warm_dir() / "price_history.json",
            {"saved_at": time.time(), "history": {k: list(v) for k, v in hist.items()}},
        )
    except Exception as exc:  # noqa: BLE001 — warm state must never break the loop
        logger.debug("warm save price_history failed: %s", exc)


def load_price_history(
    max_age_sec: float = HISTORY_MAX_AGE_SEC,
) -> dict[str, list[tuple[float, float]]]:
    """Restore fresh ticks only; stale entries (or a stale file) are dropped."""
    try:
        raw = _read_json(warm_dir() / "price_history.json")
        if not raw:
            return {}
        cutoff = time.time() - max_age_sec
        out: dict[str, list[tuple[float, float]]] = {}
        for asset, ticks in (raw.get("history") or {}).items():
            fresh = [
                (float(t), float(p))
                for t, p in ticks
                if float(t) >= cutoff and float(p) > 0
            ]
            if fresh:
                out[str(asset).upper()] = fresh
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("warm load price_history failed: %s", exc)
        return {}


# ── σ-ratio EWMA ─────────────────────────────────────────────────────────────

def save_sigma_ewma(ratios: dict[str, float]) -> None:
    try:
        _atomic_write_json(
            warm_dir() / "sigma_ewma.json",
            {"saved_at": time.time(), "ratios": {k: float(v) for k, v in ratios.items()}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("warm save sigma_ewma failed: %s", exc)


def load_sigma_ewma(max_age_sec: float = SIGMA_MAX_AGE_SEC) -> dict[str, float]:
    """Restore the EWMA map unless the snapshot is too old to trust."""
    try:
        raw = _read_json(warm_dir() / "sigma_ewma.json")
        if not raw:
            return {}
        if time.time() - float(raw.get("saved_at") or 0) > max_age_sec:
            return {}
        return {str(k).upper(): float(v) for k, v in (raw.get("ratios") or {}).items()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("warm load sigma_ewma failed: %s", exc)
        return {}
