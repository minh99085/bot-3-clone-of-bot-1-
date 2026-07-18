#!/usr/bin/env python3
"""Export the live paper fleet's trade ledgers into the repo (read-only copy).

The real out-of-sample corpus is the fleet's own ledger, not a Gamma pull
(Gamma does not retain resolved 5m/15m up/down markets). This script copies
each instance's trade_ledger.jsonl + pretrade_decisions.jsonl from the LIVE
bot's data dir into the repo under reports/paper_ledger_export/, then prints
an honest summary. Stdlib only.

It never writes to the live bot's directory — it only reads. Run from a repo
clone; point --src at the live bot's data/paper dir.

    python3 scripts/export_paper_ledger.py --src /root/bot-3/data/paper

Then commit reports/paper_ledger_export/ and push.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

SLUG_RE = re.compile(r"^(btc|eth|sol)-updown-(5m|15m)-(\d+)$")
DEST = Path("reports/paper_ledger_export")


def _count_settlements(path: Path) -> tuple[int, int]:
    """(settlements, wins) in a trade_ledger.jsonl."""
    n = w = 0
    if not path.is_file():
        return 0, 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "settlement" or rec.get("won") is not None:
            slug = str(rec.get("slug") or "")
            if not SLUG_RE.match(slug.lower()):
                continue
            n += 1
            if rec.get("won") or float(rec.get("pnl_usd") or 0) > 0:
                w += 1
    return n, w


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default="data/paper", help="live bot's data/paper dir")
    ap.add_argument("--dest", default=str(DEST))
    args = ap.parse_args(argv)

    src = Path(args.src)
    dest = Path(args.dest)
    if not src.is_dir():
        print(f"ERROR: source dir not found: {src}")
        return 2
    dest.mkdir(parents=True, exist_ok=True)

    total_settle = total_wins = 0
    instances: list[dict] = []
    for ledger in sorted(src.glob("*/trade_ledger.jsonl")):
        inst = ledger.parent.name
        out_dir = dest / inst
        out_dir.mkdir(parents=True, exist_ok=True)
        # read-only copies
        shutil.copy2(ledger, out_dir / "trade_ledger.jsonl")
        pretrade = ledger.parent / "pretrade_decisions.jsonl"
        if pretrade.is_file():
            shutil.copy2(pretrade, out_dir / "pretrade_decisions.jsonl")
        n, w = _count_settlements(ledger)
        total_settle += n
        total_wins += w
        instances.append({"instance": inst, "settlements": n, "wins": w})
        print(f"  {inst}: {n} settlements, {w} wins")

    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": str(src),
        "instances": instances,
        "total_settlements": total_settle,
        "total_wins": total_wins,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nTOTAL: {total_settle} settled real trades, {total_wins} wins "
          f"across {len(instances)} instances")
    print(f"Exported to {dest}/  (commit this dir)")
    if total_settle < 100:
        print(f"NOTE: {total_settle} < 100 settled trades — not yet enough for a "
              "go/no-go call on real edge; let the fleet keep running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
