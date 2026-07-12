"""#2 Compounding LESSONS book for the BTC pulse self-improving loop (PAPER ONLY).

Every notable event (a bucket proven losing, a context becoming a real edge, a breaker trip) appends
a timestamped LESSON -> RULE. Lessons survive restarts and are fed back into the maker (Grok) +
checker (Claude) prompts each cycle.

GRADED + SELF-RETRACTING: lessons are NOT append-only forever. A `(kind,key)` is refreshed (not
locked) so a context that flips losing<->winning UPDATES its rule; and `sync()` RETRACTS an
avoid/exploit lesson once the live evidence stops supporting it (it hasn't been re-confirmed within
``revalidate_ttl_s``). Only ACTIVE lessons are fed to the prompts, so the LLMs never read stale or
regime-obsolete rules. This is grading-by-evidence: a rule survives only while the data keeps proving it.
"""

from __future__ import annotations

import time
from typing import Optional

# kinds that are continuously re-validated against live evidence (and retracted when stale).
REVALIDATED_KINDS = ("avoid", "exploit")


class LessonsBook:
    def __init__(self, *, max_lessons: int = 300, revalidate_ttl_s: float = 21600.0):
        self.max_lessons = int(max_lessons)
        self.revalidate_ttl_s = float(revalidate_ttl_s)   # avoid/exploit rule expires if not re-confirmed
        self.lessons: list = []          # [{ts, kind, key, rule, status, last_seen_ts, refreshes}]
        self._idx: dict = {}             # (kind,key) -> lesson dict (for in-place UPDATE, not lock)
        self.retracted_total = 0

    def add(self, *, kind: str, key: str, rule: str, ts: Optional[float] = None,
            now: Optional[float] = None) -> bool:
        """Add a new lesson OR refresh/UPDATE an existing (kind,key) (re-confirm + update its rule and
        last_seen, reactivate if previously retracted). Returns True only when NEWLY added."""
        now = float(now if now is not None else time.time())
        k = (str(kind), str(key))
        existing = self._idx.get(k)
        if existing is not None:
            existing["rule"] = str(rule)[:300]
            existing["last_seen_ts"] = round(now, 1)
            existing["refreshes"] = int(existing.get("refreshes", 0)) + 1
            existing["status"] = "active"             # re-confirmed -> (re)activate
            return False
        L = {"ts": round(float(ts if ts is not None else now), 1), "kind": str(kind),
             "key": str(key), "rule": str(rule)[:300], "status": "active",
             "last_seen_ts": round(now, 1), "refreshes": 0}
        self.lessons.append(L)
        self._idx[k] = L
        if len(self.lessons) > self.max_lessons:
            drop = self.lessons.pop(0)
            self._idx.pop((drop.get("kind"), drop.get("key")), None)
        return True

    def sync(self, *, active_keys: set, now: Optional[float] = None,
             kinds: tuple = REVALIDATED_KINDS) -> dict:
        """RETRACT avoid/exploit lessons whose (kind,key) is no longer in the live ``active_keys`` AND
        hasn't been re-confirmed within ``revalidate_ttl_s`` (regime changed / evidence gone). Active
        keys passed in are refreshed by the caller's add() calls; this only expires the stale ones."""
        now = float(now if now is not None else time.time())
        retracted = []
        active_keys = active_keys or set()
        for L in self.lessons:
            if L.get("kind") in kinds and L.get("status") == "active":
                k = (L.get("kind"), L.get("key"))
                if k not in active_keys and (now - float(L.get("last_seen_ts", 0))) > self.revalidate_ttl_s:
                    L["status"] = "retracted"
                    L["retracted_ts"] = round(now, 1)
                    self.retracted_total += 1
                    retracted.append(L.get("key"))
        return {"retracted": retracted, "n": len(retracted)}

    def active(self) -> list:
        return [L for L in self.lessons if L.get("status", "active") == "active"]

    def recent(self, n: int = 12) -> list:
        """Only ACTIVE (currently evidence-backed) lessons are fed to the maker/checker prompts."""
        return self.active()[-int(n):]

    def report(self) -> dict:
        act = self.active()
        return {"count": len(self.lessons), "active": len(act),
                "retracted_total": self.retracted_total,
                "revalidate_ttl_s": self.revalidate_ttl_s, "recent": act[-12:]}

    def to_markdown(self) -> str:
        import datetime
        out = ["# Hermes BTC Pulse — LESSONS (auto-generated, compounding + self-retracting)\n",
               "_Rules survive only while live evidence keeps proving them; stale rules are retracted "
               "and not fed to the maker/checker. PAPER ONLY._\n", "\n## Active\n"]
        for ln in reversed(self.active()[-100:]):
            t = datetime.datetime.fromtimestamp(ln["ts"], datetime.UTC).strftime("%Y-%m-%d %H:%M")
            out.append("- **%s** [`%s`]: %s" % (t, ln["kind"], ln["rule"]))
        retr = [l for l in self.lessons if l.get("status") == "retracted"]
        if retr:
            out.append("\n## Retracted (no longer evidence-backed)\n")
            for ln in reversed(retr[-30:]):
                out.append("- ~~[`%s`] %s~~" % (ln["kind"], ln["rule"]))
        return "\n".join(out) + "\n"

    def to_state(self) -> dict:
        return {"lessons": list(self.lessons), "retracted_total": self.retracted_total}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.lessons = list(data.get("lessons") or [])[-self.max_lessons:]
        # backfill new fields for lessons saved by the older append-only book
        for L in self.lessons:
            L.setdefault("status", "active")
            L.setdefault("last_seen_ts", L.get("ts", 0))
            L.setdefault("refreshes", 0)
        self._idx = {(l.get("kind"), l.get("key")): l for l in self.lessons}
        self.retracted_total = int(data.get("retracted_total", 0) or 0)
