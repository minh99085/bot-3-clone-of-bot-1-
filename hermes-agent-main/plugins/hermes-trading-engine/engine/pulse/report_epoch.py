"""Report epoch: anchor published reports to a trading-reset snapshot.

Trading + learning state may span multiple eras; reports pulled for operators should
only include fills/P&L/lifecycle *since* the active report epoch (set on capital reset).
Learning graders (council, MC, selectivity, lessons) are never filtered here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


def utc_str(ts: Optional[float] = None) -> str:
    t = time.time() if ts is None else float(ts)
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t))


def make_epoch(
    *,
    ts: Optional[float] = None,
    token: str = "",
    starting_capital_usd: float = 500.0,
    note: str = "",
    backfilled: bool = False,
) -> dict:
    t = time.time() if ts is None else float(ts)
    return {
        "schema": "btc_pulse_report_epoch/1.0",
        "ts": t,
        "utc": utc_str(t),
        "token": str(token or ""),
        "starting_capital_usd": float(starting_capital_usd),
        "note": note or "Trading metrics in reports count only fills at or after this UTC time.",
        "backfilled": bool(backfilled),
    }


def epoch_ts(epoch: Optional[dict]) -> Optional[float]:
    if not epoch:
        return None
    try:
        t = float(epoch.get("ts"))
        return t if t > 0 else None
    except (TypeError, ValueError):
        return None


def _entry_ts(row: dict) -> Optional[float]:
    for key in ("entry_ts", "open_ts"):
        try:
            v = row.get(key)
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def filter_positions(positions: dict, since_ts: float) -> dict:
    """Keep positions with entry_ts >= since_ts (open_ts fallback)."""
    if since_ts is None:
        return dict(positions or {})
    out = {}
    for k, pos in (positions or {}).items():
        row = pos if isinstance(pos, dict) else getattr(pos, "__dict__", {})
        if isinstance(pos, dict):
            row = pos
        else:
            row = {
                "entry_ts": getattr(pos, "entry_ts", None),
                "open_ts": getattr(pos, "open_ts", None),
                "status": getattr(pos, "status", None),
                "won": getattr(pos, "won", None),
                "pnl_usd": getattr(pos, "pnl_usd", None),
                "side": getattr(pos, "side", None),
                "entry_price": getattr(pos, "entry_price", None),
                "size_usd": getattr(pos, "size_usd", None),
                "research": getattr(pos, "research", None),
            }
        ets = _entry_ts(row)
        if ets is None or ets >= float(since_ts):
            out[k] = pos if isinstance(pos, dict) else row
    return out


def _recompute_directional_stats(positions: dict) -> dict:
    """Minimal ledger stats from epoch-filtered settled positions."""
    trades = settled = wins = 0
    realized = 0.0
    gross_win = gross_loss = 0.0
    side_n = {"up": 0, "down": 0}
    side_wins = {"up": 0, "down": 0}
    open_positions = 0
    for pos in (positions or {}).values():
        row = pos if isinstance(pos, dict) else {}
        status = str(row.get("status") or "")
        if status == "open":
            open_positions += 1
            trades += 1
            continue
        if status != "settled":
            continue
        trades += 1
        settled += 1
        pnl = float(row.get("pnl_usd") or 0.0)
        realized += pnl
        if bool(row.get("won")):
            wins += 1
        if pnl > 0:
            gross_win += pnl
        elif pnl < 0:
            gross_loss += -pnl
        side = str(row.get("side") or "").lower()
        if side in side_n:
            side_n[side] += 1
            if bool(row.get("won")):
                side_wins[side] += 1
    wr = round(wins / settled, 4) if settled else None
    pf = round(gross_win / gross_loss, 4) if gross_loss > 0 else None
    return {
        "trades": trades,
        "settled": settled,
        "wins": wins,
        "win_rate": wr,
        "realized_pnl_usd": round(realized, 4),
        "profit_factor": pf,
        "open_positions": open_positions,
        "epoch_scoped": True,
    }


def filter_arb_state(state: dict, since_ts: float) -> dict:
    st = dict(state or {})
    pos = filter_positions(st.get("positions") or {}, since_ts)
    st["positions"] = pos
    realized = sum(float(p.get("realized_profit_usd") or p.get("profit_usd") or 0.0)
                   for p in pos.values() if (p.get("status") == "settled"))
    st["executed"] = len(pos)
    st["settled"] = sum(1 for p in pos.values() if p.get("status") == "settled")
    st["realized_profit_usd"] = round(realized, 6)
    st["epoch_scoped"] = True
    return st


def filter_dep_arb_state(state: dict, since_ts: float) -> dict:
    st = dict(state or {})
    pos = filter_positions(st.get("positions") or {}, since_ts)
    st["positions"] = pos
    realized = sum(float(p.get("realized_profit_usd") or 0.0)
                   for p in pos.values() if p.get("status") == "settled")
    st["executed"] = len(pos)
    st["settled"] = sum(1 for p in pos.values() if p.get("status") == "settled")
    st["realized_profit_usd"] = round(realized, 6)
    # Kelly calibration spans eras — learning, not re-scoped per epoch.
    st["epoch_scoped"] = True
    return st


def filter_ledger_doc(ledger: dict, epoch: Optional[dict]) -> dict:
    """Return a copy of the ledger with trading rows scoped to the report epoch."""
    since = epoch_ts(epoch)
    if since is None:
        return dict(ledger or {})
    doc = dict(ledger or {})
    raw_pos = doc.get("positions") or []
    if isinstance(raw_pos, dict):
        filtered = filter_positions(raw_pos, since)
        doc["positions"] = list(filtered.values())
        stats_pos = filtered
    else:
        kept = []
        for pos in raw_pos:
            row = pos if isinstance(pos, dict) else {}
            ets = _entry_ts(row)
            if ets is None or ets >= since:
                kept.append(pos)
        doc["positions"] = kept
        stats_pos = {str(i): p for i, p in enumerate(kept)}
    doc["stats"] = _recompute_directional_stats(stats_pos)
    doc["report_epoch"] = epoch
    return doc


def filter_score_history(history: dict, epoch: Optional[dict]) -> dict:
    since = epoch_ts(epoch)
    if since is None:
        return dict(history or {})
    out = dict(history or {})
    entries = [e for e in (out.get("entries") or [])
               if float(e.get("ts") or 0) >= since]
    out["entries"] = entries
    out["epoch_scoped"] = True
    return out


EPOCH_FILE = "REPORT_EPOCH.json"


def write_epoch_file(data_dir: Path, epoch: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / EPOCH_FILE).write_text(json.dumps(epoch, indent=1), encoding="utf-8")


def load_epoch_file(data_dir: Path) -> Optional[dict]:
    path = data_dir / EPOCH_FILE
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:  # noqa: BLE001
        return None


def backfill_from_capital_marker(data_dir: Path, *, starting_capital_usd: float = 500.0) -> Optional[dict]:
    """If capital reset ran before report_epoch existed, anchor to marker file mtime."""
    marker = data_dir / ".capital_reset_token"
    if not marker.exists():
        return None
    try:
        token = marker.read_text(encoding="utf-8").strip()
        ts = marker.stat().st_mtime
    except Exception:  # noqa: BLE001
        return None
    return make_epoch(
        ts=ts,
        token=token,
        starting_capital_usd=starting_capital_usd,
        note="Backfilled from capital-reset marker (pre-report-epoch deploy).",
        backfilled=True,
    )
