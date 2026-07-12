"""Walk-forward holdout validation for promotion scorecards (Roan loop engineering)."""

from __future__ import annotations

from typing import Optional


def split_holdout_by_entry_ts(
    positions: list,
    *,
    holdout_fraction: float = 0.3,
    min_train: int = 20,
) -> tuple[list, list]:
    """Time-ordered split: earliest (1-f) train, latest f holdout."""
    settled = sorted(
        [p for p in (positions or [])
         if (getattr(p, "status", None) == "settled"
             or (isinstance(p, dict) and p.get("status") == "settled"))],
        key=lambda p: float(getattr(p, "entry_ts", None) or p.get("entry_ts", 0) or 0),
    )
    if len(settled) < min_train + 5:
        return settled, []
    cut = max(min_train, int(len(settled) * (1.0 - holdout_fraction)))
    return settled[:cut], settled[cut:]


def _position_pnl(p) -> float:
    if isinstance(p, dict):
        for key in ("pnl_usd", "realized_profit_usd", "expected_profit_usd"):
            if p.get(key) is not None:
                return float(p.get(key) or 0)
        return 0.0
    for attr in ("pnl_usd", "realized_profit_usd", "expected_profit_usd"):
        val = getattr(p, attr, None)
        if val is not None:
            return float(val or 0)
    return 0.0


def _position_won(p) -> bool:
    if isinstance(p, dict) and p.get("won") is not None:
        return bool(p.get("won"))
    if not isinstance(p, dict) and getattr(p, "won", None) is not None:
        return bool(p.won)
    return _position_pnl(p) > 0


def holdout_metrics(positions: list) -> dict:
    n = len(positions)
    if not n:
        return {"n": 0, "win_rate": None, "pnl_usd": 0.0, "profit_factor": None}
    wins = 0
    pnl = 0.0
    gw = 0.0
    gl = 0.0
    for p in positions:
        pu = _position_pnl(p)
        won = _position_won(p)
        pnl += pu
        if won:
            wins += 1
            if pu > 0:
                gw += pu
        elif pu < 0:
            gl += -pu
    if gl > 0:
        pf = round(gw / gl, 4)
    elif gw > 0:
        pf = 999.0
    else:
        pf = None
    return {
        "n": n,
        "win_rate": round(wins / n, 4),
        "pnl_usd": round(pnl, 4),
        "profit_factor": pf,
    }


def passes_walk_forward(
    positions: list,
    *,
    min_holdout_n: int = 10,
    min_holdout_pf: float = 1.0,
    holdout_fraction: float = 0.3,
) -> dict:
    train, holdout = split_holdout_by_entry_ts(positions, holdout_fraction=holdout_fraction)
    hm = holdout_metrics(holdout)
    ok = (hm["n"] >= min_holdout_n
          and hm.get("profit_factor") is not None
          and float(hm["profit_factor"]) >= min_holdout_pf)
    return {
        "passed": ok,
        "train_n": len(train),
        "holdout": hm,
        "min_holdout_n": min_holdout_n,
        "min_holdout_pf": min_holdout_pf,
    }