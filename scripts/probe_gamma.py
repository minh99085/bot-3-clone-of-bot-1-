#!/usr/bin/env python3
"""Diagnostic probe for the Polymarket Gamma API — confirms the real shape of
historical crypto up/down markets so the corpus pull can be calibrated.

Zero repo dependencies (only httpx + stdlib) so it can't hit import issues.
Run on a box with network access to gamma-api.polymarket.com:

    .venv/bin/python scripts/probe_gamma.py

It fetches a handful of PAST up/down windows by exact slug (the same way the
live bot discovers current ones) and dumps the full field shape of whatever
comes back. Paste the entire output back.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com/markets"
STEP = {"5m": 300, "15m": 900}
SERIES = [
    ("btc", "5m", "btc-updown-5m-"),
    ("eth", "5m", "eth-updown-5m-"),
    ("sol", "5m", "sol-updown-5m-"),
    ("btc", "15m", "btc-updown-15m-"),
]
# How far back to probe (in number of windows): 1 window, 1 hour, 6h, 1 day, 3 days
BACK_WINDOWS = [1, 12, 72, 288, 864]

FIELDS = [
    "id", "slug", "question", "closed", "active", "startDate", "endDate",
    "outcomes", "outcomePrices", "clobTokenIds", "umaResolutionStatus",
    "volumeNum", "liquidityNum", "conditionId",
]


def aligned_now(step: int) -> int:
    return (int(datetime.now(timezone.utc).timestamp()) // step) * step


def fetch(client: httpx.Client, params: dict) -> tuple[int, object]:
    try:
        r = client.get(GAMMA, params=params)
        return r.status_code, (r.json() if r.status_code == 200 else r.text[:200])
    except httpx.HTTPError as exc:
        return -1, str(exc)


def main() -> int:
    print("=== Gamma probe:", datetime.now(timezone.utc).isoformat(), "===")
    with httpx.Client(timeout=30.0, headers={"User-Agent": "hermes-probe/1.0"}) as client:
        # 1) Exact-slug fetch of past windows (the bot's discovery method)
        print("\n--- exact-slug fetch of PAST windows ---")
        found_any = False
        for asset, tf, prefix in SERIES:
            step = STEP[tf]
            base = aligned_now(step)
            for back in BACK_WINDOWS:
                ts = base - back * step
                slug = f"{prefix}{ts}"
                code, data = fetch(client, {"slug": slug})
                rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
                ago = f"{back}w({back*step//60}min)"
                if code == 200 and rows:
                    found_any = True
                    row = rows[0]
                    summary = {k: row.get(k) for k in FIELDS if k in row}
                    print(f"\n[FOUND] {slug}  ({ago} ago)")
                    print(json.dumps(summary, indent=2)[:1400])
                else:
                    n = len(rows) if isinstance(rows, list) else 0
                    print(f"[none ] {slug}  ({ago} ago)  http={code} rows={n}")
                time.sleep(0.2)
        if not found_any:
            print("\n!! No past windows found by exact slug — slug format or "
                  "endpoint differs from the live bot's assumption.")

        # 2) What DO closed crypto markets look like? Try a couple of listing filters.
        print("\n--- listing probes (to see real slugs of closed crypto markets) ---")
        for label, params in [
            ("closed+limit", {"closed": "true", "limit": 5}),
            ("tag=crypto", {"closed": "true", "limit": 5, "tag": "crypto"}),
            ("tag_slug=crypto", {"closed": "true", "limit": 5, "tag_slug": "crypto"}),
            ("slug-search-btc", {"closed": "true", "limit": 5, "slug": "btc-updown-5m"}),
        ]:
            code, data = fetch(client, params)
            n = len(data) if isinstance(data, list) else 0
            print(f"\n[{label}] http={code} rows={n}")
            if isinstance(data, list):
                for row in data[:5]:
                    print("   slug=", row.get("slug"), "| closed=", row.get("closed"),
                          "| end=", row.get("endDate"))
            elif code != 200:
                print("   body:", data)
            time.sleep(0.3)

        # 3) Offset ceiling check (the 422 we hit)
        print("\n--- offset ceiling probe ---")
        for off in (0, 1000, 2000, 2100, 5000):
            code, _ = fetch(client, {"closed": "true", "limit": 100, "offset": off})
            print(f"   offset={off}: http={code}")
            time.sleep(0.2)
    print("\n=== probe done — paste all output above ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
