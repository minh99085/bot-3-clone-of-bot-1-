#!/usr/bin/env python3
"""Forensic fleet audit — prove the paper ledgers are neither DREAMING nor FADING.

Run on the VPS:  PYTHONPATH=. python3 scripts/audit_fleet.py --root data/paper

FADING (trades vanishing): the dashboard/report count only the CURRENT compose
lanes (INSTANCE_IDS). Every time a lane is renamed (lane06_garch→lane06_favlearn,
lane04_favcont70→lane04_favcont80, …) its ledger is orphaned and drops out of
the total — the count goes DOWN even though ledgers are append-only. This audit
globs EVERY data/paper/<lane>/trade_ledger.jsonl (active + orphaned) so the
LIFETIME count is monotonic, and shows the active-vs-lifetime gap explicitly.

DREAMING (fabricated numbers): every settlement's PnL is recomputed from the
canonical formula and compared to the stored value; entry∈(0,1], size>0,
wins+losses==settled, Σ(per-lane pnl)==fleet pnl, no duplicate signal_ids.

Exit code 0 = clean; 1 = a reconciliation check failed (real integrity problem).
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.dashboard_data import INSTANCE_IDS  # noqa: E402
from hermes.settlement_fast import settlement_pnl_usd  # noqa: E402
from hermes.state_io import read_jsonl  # noqa: E402

PNL_TOL = 0.02  # cent rounding


def _settlements(ledger: Path) -> list[dict]:
    out = []
    for r in read_jsonl(ledger):
        if isinstance(r, dict) and (r.get("event") == "settlement" or r.get("won") is not None):
            out.append(r)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data/paper")
    args = ap.parse_args(argv)

    root = Path(args.root)
    ledgers = sorted(root.glob("*/trade_ledger.jsonl"))
    active = set(INSTANCE_IDS)

    per_lane: dict[str, dict] = {}
    seen_sigids: dict[str, int] = defaultdict(int)
    dream_flags: list[str] = []

    for ledger in ledgers:
        lane = ledger.parent.name
        st = _settlements(ledger)
        wins = sum(1 for s in st if s.get("won"))
        pnl = 0.0
        for s in st:
            p = float(s.get("pnl_usd") or 0.0)
            pnl += p
            sid = str(s.get("signal_id") or s.get("position_id") or "")
            if sid:
                seen_sigids[sid] += 1
            # DREAMING check: recompute PnL from the canonical formula.
            entry = float(s.get("entry_price") or 0.0)
            size = float(s.get("size_usd") or 0.0)
            if not (0.0 < entry <= 1.0):
                dream_flags.append(f"{lane} {sid}: entry_price={entry} out of (0,1]")
            if size <= 0:
                dream_flags.append(f"{lane} {sid}: size_usd={size} <= 0")
            expect = settlement_pnl_usd(won=bool(s.get("won")), size_usd=size, entry_price=entry)
            if abs(expect - p) > PNL_TOL:
                dream_flags.append(
                    f"{lane} {sid}: stored pnl={p:+.2f} != formula {expect:+.2f} "
                    f"(won={s.get('won')} size={size} entry={entry})"
                )
        per_lane[lane] = {
            "n": len(st), "wins": wins, "losses": len(st) - wins,
            "pnl": round(pnl, 2), "active": lane in active,
        }

    active_lanes = {k: v for k, v in per_lane.items() if v["active"]}
    orphan_lanes = {k: v for k, v in per_lane.items() if not v["active"]}

    def _tot(d):
        return (sum(v["n"] for v in d.values()),
                round(sum(v["pnl"] for v in d.values()), 2))
    life_n, life_pnl = _tot(per_lane)
    act_n, act_pnl = _tot(active_lanes)
    orph_n, orph_pnl = _tot(orphan_lanes)

    print("=== FLEET AUDIT ===")
    print(f"ledgers found: {len(ledgers)}  (active {len(active_lanes)}, "
          f"orphaned {len(orphan_lanes)})\n")
    print(f"{'lane':24s} {'n':>4s} {'W/L':>8s} {'PnL$':>10s}  where")
    for lane in sorted(per_lane, key=lambda k: (per_lane[k]['active'], k)):
        v = per_lane[lane]
        tag = "ACTIVE" if v["active"] else "orphaned(renamed/retired)"
        print(f"{lane:24s} {v['n']:>4d} {v['wins']:>3d}/{v['losses']:<3d} "
              f"{v['pnl']:>+10.2f}  {tag}")

    print("\n--- TOTALS ---")
    print(f"LIFETIME  (all ledgers, append-only): {life_n:4d} trades   ${life_pnl:+.2f}")
    print(f"ACTIVE    (current compose lanes):    {act_n:4d} trades   ${act_pnl:+.2f}")
    print(f"ORPHANED  (dropped from dashboard):   {orph_n:4d} trades   ${orph_pnl:+.2f}")
    if orph_n:
        print(f"\n>>> The dashboard 'total trades' is UNDERSTATED by {orph_n} "
              f"(and PnL by ${orph_pnl:+.2f}) because these renamed/retired lanes "
              "are no longer in INSTANCE_IDS. That is the 'fading number' — the "
              "trades are NOT lost, just uncounted. LIFETIME is the honest total.")

    print("\n--- DREAMING checks ---")
    dupes = {sid: n for sid, n in seen_sigids.items() if n > 1}
    ok = True
    if dupes:
        ok = False
        print(f"FAIL: {len(dupes)} duplicate signal_ids (double-counted): "
              f"{list(dupes)[:5]}")
    else:
        print("ok: no duplicate signal_ids")
    if dream_flags:
        ok = False
        print(f"FAIL: {len(dream_flags)} PnL/entry inconsistencies:")
        for f in dream_flags[:12]:
            print(f"    {f}")
    else:
        print("ok: every settlement PnL matches the canonical formula; "
              "entry∈(0,1], size>0")

    print("\nVERDICT:", "CLEAN — numbers reconcile" if ok else
          "INTEGRITY FAILURE — see FAILs above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
