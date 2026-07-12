"""MEMORY.md — disk-bound long-term loop memory (Osmani Loop Engineering #2).

The bot reads this file on wake and updates it on each ledger persist. It complements
LESSONS.md (graded rules) and STATE.md (snapshot) with durable cross-interval context:
lane health, last decisions, capital posture, and active constraints.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any, Optional


def _utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _section(body: str, name: str) -> str:
    pat = rf"(?ms)^## {re.escape(name)}\n.*?(?=^## |\Z)"
    m = re.search(pat, body)
    return m.group(0).strip() if m else ""


class LoopMemory:
    """Read/write MEMORY.md at ``data_dir / MEMORY.md``."""

    FILENAME = "MEMORY.md"

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / self.FILENAME
        self.data: dict[str, Any] = {
            "last_wake_ts": None,
            "wake_count": 0,
            "lane_status": {},
            "capital_snapshot": {},
            "recent_decisions": [],
            "active_constraints": [],
            "notes": [],
        }

    def load(self) -> dict:
        """Load MEMORY.md on wake; tolerate missing or corrupt files."""
        if not self.path.exists():
            self.data["wake_count"] = 0
            return dict(self.data)
        try:
            text = self.path.read_text(encoding="utf-8")
            self._parse(text)
        except Exception:
            pass
        self.data["wake_count"] = int(self.data.get("wake_count") or 0) + 1
        self.data["last_wake_ts"] = _utc_now()
        return dict(self.data)

    def _parse(self, text: str) -> None:
        meta = _section(text, "Meta")
        for line in meta.splitlines():
            if line.startswith("- **wake_count:**"):
                try:
                    self.data["wake_count"] = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
            if line.startswith("- **last_wake:**"):
                self.data["last_wake_ts"] = line.split(":", 1)[-1].strip()

        constraints = _section(text, "Active constraints")
        self.data["active_constraints"] = [
            ln.strip("- ").strip()
            for ln in constraints.splitlines()
            if ln.strip().startswith("-") and ln.strip() != "-"
        ]

        recent = _section(text, "Recent decisions")
        rows = []
        for ln in recent.splitlines():
            if ln.strip().startswith("|") and "---" not in ln and "event_id" not in ln:
                parts = [c.strip() for c in ln.strip("|").split("|")]
                if len(parts) >= 4:
                    rows.append({
                        "ts": parts[0], "event_id": parts[1],
                        "side": parts[2], "status": parts[3],
                    })
        if rows:
            self.data["recent_decisions"] = rows[-20:]

    def record_triage(self, *, token_id: str, time_boundary: str, status: str,
                      symbol: str = "", timeframe: str = "", side: str = "") -> None:
        """Skill §4 — persist triage handoff to disk (context rot prevention)."""
        row = {
            "ts": _utc_now(),
            "token_id": str(token_id)[:64],
            "time_boundary": str(time_boundary)[:32],
            "status": status,
            "symbol": symbol,
            "timeframe": timeframe,
            "side": side,
        }
        triage = list(self.data.get("recent_triage") or [])
        triage.append(row)
        self.data["recent_triage"] = triage[-20:]
        self.record_decision(
            event_id=f"{symbol}@{timeframe}" if symbol else token_id[:28],
            side=side or "—",
            status=status,
            detail=f"token={token_id[:20]} boundary={time_boundary}",
        )

    def record_decision(self, *, event_id: str, side: str, status: str,
                        detail: str = "") -> None:
        row = {"ts": _utc_now(), "event_id": event_id, "side": side,
               "status": status, "detail": detail[:120]}
        recent = list(self.data.get("recent_decisions") or [])
        recent.append(row)
        self.data["recent_decisions"] = recent[-20:]

    def update_lane_status(self, lane_status: dict) -> None:
        self.data["lane_status"] = dict(lane_status or {})

    def update_capital(self, capital: dict) -> None:
        self.data["capital_snapshot"] = dict(capital or {})

    def add_note(self, note: str) -> None:
        notes = list(self.data.get("notes") or [])
        notes.append(f"- **{_utc_now()}** {note[:300]}")
        self.data["notes"] = notes[-15:]

    def to_markdown(self) -> str:
        d = self.data
        cap = d.get("capital_snapshot") or {}
        lanes = d.get("lane_status") or {}
        lines = [
            "# Bot-1 Loop Memory (MEMORY.md)",
            "",
            "_Disk-bound long-term memory. Read on wake; updated each ledger persist. PAPER ONLY._",
            "",
            "## Meta",
            f"- **last_wake:** {d.get('last_wake_ts') or '—'}",
            f"- **wake_count:** {int(d.get('wake_count') or 0)}",
            f"- **schema:** osmani_loop_memory/1.0",
            "",
            "## Active constraints",
        ]
        for c in (d.get("active_constraints") or [
            "PAPER ONLY — live_trading_enabled must stay false",
            "Loop Engineering architecture LOCKED — ask operator before lane/optimizer/MEMORY-schema changes",
            "Honest accounting — no inflated arb/dep-arb P&L",
            "Maker-checker — trade failed until independent API verify",
        ]):
            lines.append(f"- {c}")
        lines += [
            "",
            "## Capital snapshot",
            f"- **on_hand_usd:** {cap.get('on_hand_capital_usd', '—')}",
            f"- **open_exposure_usd:** {cap.get('open_exposure_usd', '—')}",
            f"- **realized_pnl_usd:** {cap.get('total_realized_pnl_usd', cap.get('realized_pnl_usd', '—'))}",
            "",
            "## Lane status",
        ]
        for name, st in sorted(lanes.items()):
            if isinstance(st, dict):
                lines.append(f"- **{name}:** {json.dumps(st, default=str)[:200]}")
            else:
                lines.append(f"- **{name}:** {st}")
        lines += [
            "",
            "## Recent decisions",
            "",
            "| ts | event_id | side | status |",
            "|----|----------|------|--------|",
        ]
        for r in (d.get("recent_decisions") or [])[-10:]:
            lines.append("| {ts} | {event_id} | {side} | {status} |".format(**{
                "ts": r.get("ts", "—"),
                "event_id": str(r.get("event_id", "—"))[:28],
                "side": r.get("side", "—"),
                "status": r.get("status", "—"),
            }))
        if not d.get("recent_decisions"):
            lines.append("| — | — | — | — |")
        triage = d.get("recent_triage") or []
        if triage:
            lines += [
                "",
                "## Recent triage (Discovery skill)",
                "",
                "| ts | symbol | tf | side | status | token_id |",
                "|----|--------|----|------|--------|----------|",
            ]
            for r in triage[-10:]:
                lines.append(
                    "| {ts} | {symbol} | {timeframe} | {side} | {status} | {token_id} |".format(
                        ts=r.get("ts", "—"),
                        symbol=r.get("symbol", "—"),
                        timeframe=r.get("timeframe", "—"),
                        side=r.get("side", "—"),
                        status=r.get("status", "—"),
                        token_id=str(r.get("token_id", "—"))[:24],
                    )
                )
        notes = d.get("notes") or []
        if notes:
            lines += ["", "## Notes", ""]
            lines.extend(notes[-10:])
        lines.append("")
        return "\n".join(lines)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.to_markdown(), encoding="utf-8")
