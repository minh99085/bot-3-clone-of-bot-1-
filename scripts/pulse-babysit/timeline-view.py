#!/usr/bin/env python3
"""View monitoring/timeline.jsonl — trends and diffs."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TIMELINE = ROOT / "monitoring" / "timeline.jsonl"


def _load_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return iso[:16]


def _print_table(rows: list[dict]) -> None:
    hdr = (
        f"{'UTC':<12} {'trades':>6} {'WR':>6} {'PnL':>8} {'ticks':>6} "
        f"{'TV':>4} {'MTF':>12} {'top_block':>18} {'changed':>7}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        led = r.get("ledger") or {}
        tv = r.get("tv") or {}
        funnel = r.get("funnel") or {}
        top = funnel.get("top_rejects") or {}
        top_name = next(iter(top), "—") if top else "—"
        wr = led.get("win_rate")
        wr_s = f"{float(wr)*100:.0f}%" if wr is not None else "—"
        pnl = led.get("pnl_usd")
        pnl_s = f"{float(pnl):+.1f}" if pnl is not None else "—"
        ch = "YES" if r.get("config_changed") else ""
        print(
            f"{_fmt_ts(r.get('ts_utc','')):<12} "
            f"{led.get('trades') or 0:>6} {wr_s:>6} {pnl_s:>8} "
            f"{(r.get('runtime') or {}).get('ticks') or 0:>6} "
            f"{tv.get('alerts_valid') or 0:>4} "
            f"{str(tv.get('mtf_verdict') or '—')[:12]:>12} "
            f"{str(top_name)[:18]:>18} {ch:>7}"
        )


def _print_diff(a: dict, b: dict) -> None:
    print(f"A: {_fmt_ts(a.get('ts_utc',''))}  B: {_fmt_ts(b.get('ts_utc',''))}")
    keys = [
        ("design", "design"),
        ("ledger.trades", lambda r: (r.get("ledger") or {}).get("trades")),
        ("ledger.win_rate", lambda r: (r.get("ledger") or {}).get("win_rate")),
        ("funnel.cohort_session_blocks", lambda r: (r.get("funnel") or {}).get("cohort_session_blocks")),
        ("recent_evals", lambda r: r.get("recent_evals")),
        ("config_fingerprint", lambda r: r.get("config_fingerprint")),
    ]
    for label, getter in keys:
        if callable(getter):
            va, vb = getter(a), getter(b)
        else:
            va, vb = a.get(getter), b.get(getter)
        if va != vb:
            print(f"  {label}:")
            print(f"    was: {va}")
            print(f"    now: {vb}")


def main() -> int:
    ap = argparse.ArgumentParser(description="View bot monitoring timeline")
    ap.add_argument("-n", "--last", type=int, default=24, help="Show last N snapshots")
    ap.add_argument("--diff", action="store_true", help="Diff last two snapshots")
    ap.add_argument("--json", action="store_true", help="Print last snapshot as JSON")
    args = ap.parse_args()

    rows = _load_lines(TIMELINE)
    if not rows:
        print(f"No timeline yet. Run: python scripts/pulse-babysit/record-timeline.py")
        return 1

    if args.json:
        print(json.dumps(rows[-1], indent=2, default=str))
        return 0

    if args.diff and len(rows) >= 2:
        _print_diff(rows[-2], rows[-1])
        return 0

    _print_table(rows[-args.last :])
    print(f"\n({len(rows)} total snapshots in {TIMELINE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())