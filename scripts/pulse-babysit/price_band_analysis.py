#!/usr/bin/env python3
"""24h DOWN-only ledger analysis by entry price band. Library + CLI."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "vps_full_reports" / "latest" / "btc_pulse_ledger.json"
DEFAULT_POLICY = Path(__file__).resolve().parent / "wr-tune-policy.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _band_for_price(price: float, bands: dict) -> str | None:
    cheap = bands.get("cheap") or {}
    sweet = bands.get("sweet") or {}
    expensive = bands.get("expensive") or {}
    if price < float(cheap.get("max_exclusive", 0.45)):
        return "cheap"
    if (float(sweet.get("min_inclusive", 0.45)) <= price
            <= float(sweet.get("max_inclusive", 0.55))):
        return "sweet"
    if price > float(expensive.get("min_exclusive", 0.55)):
        return "expensive"
    return None


def _empty_band() -> dict[str, Any]:
    return {"n": 0, "wins": 0, "win_rate": None, "pnl_usd": 0.0}


def analyze_price_bands(
    ledger: dict,
    *,
    lookback_hours: float = 24.0,
    side: str = "down",
    now_ts: float | None = None,
    policy: dict | None = None,
) -> dict[str, Any]:
    """Return per-band stats for settled positions within lookback window."""
    policy = policy or _load_json(DEFAULT_POLICY)
    bands_cfg = policy.get("bands") or {}
    side = (side or "down").lower()
    now_ts = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - (lookback_hours * 3600.0)

    band_stats: dict[str, dict[str, Any]] = {
        "cheap": _empty_band(),
        "sweet": _empty_band(),
        "expensive": _empty_band(),
    }
    total_n = 0
    total_wins = 0
    total_pnl = 0.0

    for pos in ledger.get("positions") or []:
        if (pos.get("status") or "").lower() != "settled":
            continue
        if (pos.get("side") or "").lower() != side:
            continue
        try:
            entry_ts = float(pos.get("entry_ts") or pos.get("open_ts") or 0)
            entry_price = float(pos.get("entry_price") or 0)
            pnl = float(pos.get("pnl_usd") or 0)
            won = bool(pos.get("won"))
        except (TypeError, ValueError):
            continue
        if entry_ts < cutoff:
            continue

        band = _band_for_price(entry_price, bands_cfg)
        if band is None:
            continue

        b = band_stats[band]
        b["n"] += 1
        if won:
            b["wins"] += 1
        b["pnl_usd"] = round(float(b["pnl_usd"]) + pnl, 4)
        total_n += 1
        if won:
            total_wins += 1
        total_pnl += pnl

    for b in band_stats.values():
        if b["n"] > 0:
            b["win_rate"] = round(b["wins"] / b["n"], 4)
        b["pnl_usd"] = round(b["pnl_usd"], 2)

    overall_wr = round(total_wins / total_n, 4) if total_n else None
    sweet_share = (band_stats["sweet"]["n"] / total_n) if total_n else None

    return {
        "lookback_hours": lookback_hours,
        "side": side,
        "cutoff_ts": cutoff,
        "now_ts": now_ts,
        "total": {
            "n": total_n,
            "wins": total_wins,
            "win_rate": overall_wr,
            "pnl_usd": round(total_pnl, 2),
        },
        "bands": band_stats,
        "sweet_share": round(sweet_share, 4) if sweet_share is not None else None,
    }


def detect_band_issues(
    analysis: dict,
    policy: dict | None = None,
) -> list[dict[str, str]]:
    """Map band stats to babysit issue dicts (code, severity, detail, hint)."""
    policy = policy or _load_json(DEFAULT_POLICY)
    triggers = policy.get("triggers") or {}
    bands = analysis.get("bands") or {}
    total_n = int((analysis.get("total") or {}).get("n") or 0)
    sweet = bands.get("sweet") or {}
    issues: list[dict[str, str]] = []

    cheap_trig = triggers.get("cheap_down_bleed") or {}
    cheap = bands.get("cheap") or {}
    if (cheap.get("n", 0) >= int(cheap_trig.get("min_samples", 2))
            and cheap.get("win_rate") is not None
            and float(cheap["win_rate"]) < float(cheap_trig.get("max_win_rate", 0.40))):
        issues.append({
            "code": cheap_trig.get("issue_code", "cheap_down_bleed"),
            "severity": "P1",
            "detail": (f"cheap_band n={cheap['n']} wr={cheap['win_rate']} "
                       f"pnl={cheap.get('pnl_usd')} (24h DOWN)"),
            "hint": "raise PULSE_MIN_ENTRY_PRICE — never below 0.45",
        })

    exp_trig = triggers.get("expensive_down_bleed") or {}
    expensive = bands.get("expensive") or {}
    if (expensive.get("n", 0) >= int(exp_trig.get("min_samples", 5))
            and expensive.get("win_rate") is not None
            and float(expensive["win_rate"]) < float(exp_trig.get("max_win_rate", 0.58))
            and float(expensive.get("pnl_usd") or 0) <= float(exp_trig.get("max_pnl_usd", 0))):
        issues.append({
            "code": exp_trig.get("issue_code", "expensive_down_bleed"),
            "severity": "P1",
            "detail": (f"expensive_band n={expensive['n']} wr={expensive['win_rate']} "
                       f"pnl={expensive.get('pnl_usd')} (24h DOWN)"),
            "hint": "lower PULSE_MAX_PRICE toward sweet-spot ceiling",
        })

    sweet_trig = triggers.get("sweet_spot_underuse") or {}
    sweet_wr = sweet.get("win_rate")
    sweet_n = int(sweet.get("n") or 0)
    sweet_share = analysis.get("sweet_share")
    if (total_n >= 8 and sweet_n >= int(sweet_trig.get("min_sweet_samples", 5))
            and sweet_wr is not None
            and float(sweet_wr) >= float(sweet_trig.get("min_sweet_wr", 0.70))
            and sweet_share is not None
            and float(sweet_share) < float(sweet_trig.get("max_sweet_share", 0.50))):
        issues.append({
            "code": sweet_trig.get("issue_code", "sweet_spot_underuse"),
            "severity": "P2",
            "detail": (f"sweet_band wr={sweet_wr} share={sweet_share} "
                       f"n={sweet_n} total={total_n} (24h DOWN)"),
            "hint": "tighten min_entry/max_price toward 0.45-0.55 sweet spot",
        })

    return issues


def main() -> int:
    ledger_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LEDGER
    ledger = _load_json(ledger_path)
    policy = _load_json(DEFAULT_POLICY)
    analysis = analyze_price_bands(ledger, policy=policy)
    issues = detect_band_issues(analysis, policy)
    out = {"analysis": analysis, "issues": issues}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())