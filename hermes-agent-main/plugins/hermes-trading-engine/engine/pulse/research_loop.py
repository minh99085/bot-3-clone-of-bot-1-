"""#4 Research meta-loop (the `/goal` layer) for the BTC pulse system (PAPER ONLY).

A higher-order loop that periodically reads the FULL performance report + the compounding lessons
and uses an independent model (Claude) to propose: which contexts to exploit/avoid next, bounded
knob nudges, and new lessons to record. Recommendations are OBSERVE-ONLY by default; an optional,
whitelisted + bounded auto-apply (with a verifiable guard) can act on them. This is loop-engineering
at the strategy layer — the system tuning itself between cycles, not just per-window.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

from engine.pulse.grok_intel import _parse_json, GrokBudget
from engine.pulse.claude_client import claude_chat


# the EXACT per-candidate dimensions the live gate can act on (so avoid_contexts are applicable)
GATEABLE_DIMS = ("hurst_regime", "zscore_bucket", "ttc_bucket", "confidence_tier", "spread_bucket",
                 "depth_bucket", "markov_state", "edge_quality_bucket", "stale_divergence")

# Whitelisted directional knobs the research loop may recommend (bounded; engine auto-apply stays off).
DIRECTIONAL_KNOBS = (
    "PULSE_DIRECTIONAL_EXPLORE_RATE",
    "PULSE_LLM_COUNCIL_MIN_AGREEMENT",
    "PULSE_LLM_COUNCIL_MIN_MARGIN",
)


def _clean_context(x) -> Optional[str]:
    """Normalize a model-proposed context to a clean ``dim=bucket`` (strip prose like
    'edge_quality=high (20% win, n=12)' -> 'edge_quality=high'); return None if not parseable."""
    s = str(x).strip()
    if "=" not in s:
        return None
    dim, _, bucket = s.partition("=")
    dim = dim.strip().lower()
    bucket = bucket.strip()
    for sep in (" (", "(", " "):
        if sep in bucket:
            bucket = bucket.split(sep, 1)[0]
    bucket = bucket.strip().strip(",").strip()
    return ("%s=%s" % (dim, bucket)) if dim and bucket else None


def make_research_fn(*, model: Optional[str] = None, timeout_s: float = 30.0, chat=claude_chat):
    box: dict = {}
    system = ("You are a quant research lead reviewing a PAPER BTC directional bot's full "
              "performance report + its compounding LESSONS. Be data-driven and skeptical: "
              "directional crypto is near-efficient, so do NOT assume edge that the data does not show. "
              "Recommend concrete next steps: directional exploit/avoid contexts, bounded knob nudges "
              "ONLY from allowed_knobs when sample-backed, and new lessons. Never recommend going live.")

    def _research(report: dict) -> Optional[dict]:
        prompt = ("Analyze and respond with STRICT JSON ONLY: {\"summary\":\"<2-3 sentences>\","
                  "\"exploit_contexts\":[\"dim=bucket\"],\"avoid_contexts\":[\"dim=bucket\"],"
                  "\"knob_recommendations\":[{\"knob\":\"<name>\",\"value\":<num>,\"reason\":\"<short>\"}],"
                  "\"new_lessons\":[{\"key\":\"<unique>\",\"rule\":\"<short>\"}]}.\n"
                  "For avoid_contexts/exploit_contexts use ONLY these dimensions and a BARE "
                  "'dim=bucket' (NO prose, NO stats in the string): " + ", ".join(GATEABLE_DIMS)
                  + ". Example: \"avoid_contexts\":[\"hurst_regime=trending\",\"ttc_bucket=<60s\"].\n"
                  "For knob_recommendations, knob MUST be one of: "
                  + ", ".join(DIRECTIONAL_KNOBS)
                  + " and value must be numeric.\n"
                  "REPORT: " + json.dumps(report, default=str)[:10000])
        d = _parse_json(chat(prompt, model=model, timeout_s=timeout_s, box=box, system=system,
                             max_tokens=1500))
        if not d:
            return None
        ex = [c for c in (_clean_context(x) for x in (d.get("exploit_contexts") or [])) if c][:10]
        av = [c for c in (_clean_context(x) for x in (d.get("avoid_contexts") or [])) if c][:10]
        knobs = []
        allowed = set(DIRECTIONAL_KNOBS)
        for r in (d.get("knob_recommendations") or []):
            if not isinstance(r, dict):
                continue
            knob = str(r.get("knob") or "")[:60]
            if knob.startswith("PULSE_") and knob not in allowed:
                continue
            knobs.append({"knob": knob[:40], "value": r.get("value"),
                          "reason": str(r.get("reason"))[:160]})
        return {"summary": str(d.get("summary", ""))[:1000],
                "exploit_contexts": ex, "avoid_contexts": av,
                "knob_recommendations": knobs[:8],
                "new_lessons": [{"key": str(x.get("key"))[:60], "rule": str(x.get("rule"))[:300]}
                                for x in (d.get("new_lessons") or []) if isinstance(x, dict)][:8]}
    return _research


class ResearchLoop:
    def __init__(self, *, research_fn=None, budget: Optional[GrokBudget] = None,
                 interval_s: float = 1800.0, event_min_gap_s: float = 600.0,
                 report_provider: Optional[Callable[[], dict]] = None,
                 lessons=None, apply_fn: Optional[Callable[[dict], list]] = None,
                 auto_apply: bool = False):
        self._fn = research_fn if research_fn is not None else make_research_fn()
        self._budget = budget
        self.interval_s = max(300.0, float(interval_s))          # idle FLOOR (run at least this often)
        self.event_min_gap_s = max(60.0, float(event_min_gap_s))  # min gap between event-triggered runs
        self._report_provider = report_provider
        self._lessons = lessons
        self._apply_fn = apply_fn
        self.auto_apply = bool(auto_apply)
        self._lock = threading.Lock()
        self._note: Optional[dict] = None
        self._note_ts = 0.0
        self._applied: list = []
        self.calls = 0
        self.errors = 0
        self.skipped_budget = 0
        self.lessons_added = 0
        self.event_runs = 0
        self.triggers: dict = {}
        self._pending_event: Optional[str] = None
        self._last_run_ts = 0.0
        self._last_trigger: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def request_run(self, reason: str) -> None:
        """Ask the meta-loop to re-analyze SOON because something material changed (new edge, breaker
        trip, new avoid-rule, fresh samples). Respects event_min_gap_s so it never spams Claude."""
        with self._lock:
            self._pending_event = str(reason)

    def refresh(self, trigger: str = "manual") -> Optional[dict]:
        self._last_trigger = trigger
        self._last_run_ts = time.time()
        if self._budget is not None and not self._budget.try_spend("research"):
            self.skipped_budget += 1
            return None
        report = {}
        try:
            report = self._report_provider() if self._report_provider else {}
        except Exception:  # noqa: BLE001
            report = {}
        note = None
        try:
            note = self._fn(report)
        except Exception:  # noqa: BLE001
            note = None
        if note is None:
            self.errors += 1
            return None
        self.calls += 1
        # record any new lessons the research proposed (compounding skill)
        if self._lessons is not None:
            for ls in note.get("new_lessons", []):
                if ls.get("key") and ls.get("rule") and self._lessons.add(
                        kind="research", key=ls["key"], rule=ls["rule"]):
                    self.lessons_added += 1
        applied = []
        if self.auto_apply and self._apply_fn is not None:
            try:
                applied = self._apply_fn(note) or []
            except Exception:  # noqa: BLE001 — applying never breaks the loop
                applied = []
        with self._lock:
            self._note, self._note_ts = note, time.time()
            if applied:
                self._applied = (self._applied + applied)[-30:]
        return note

    def _worker(self) -> None:
        self._stop.wait(min(self.interval_s, 90.0))
        while not self._stop.is_set():
            try:
                now = time.time()
                due = (now - self._last_run_ts) >= self.interval_s          # idle floor reached
                ev = None
                with self._lock:
                    if (self._pending_event is not None
                            and (now - self._last_run_ts) >= self.event_min_gap_s):
                        ev = self._pending_event
                        self._pending_event = None
                if due or ev is not None:
                    if ev is not None:
                        self.event_runs += 1
                        self.triggers[ev] = self.triggers.get(ev, 0) + 1
                    self.refresh(trigger=(ev or "interval"))
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(min(30.0, self.interval_s))   # check often; actual runs are gap-limited

    def start(self) -> "ResearchLoop":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="research-loop", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def report(self) -> dict:
        with self._lock:
            return {"enabled": True, "interval_floor_s": self.interval_s,
                    "event_min_gap_s": self.event_min_gap_s, "auto_apply": self.auto_apply,
                    "calls": self.calls, "event_runs": self.event_runs, "triggers": dict(self.triggers),
                    "pending_event": self._pending_event, "last_trigger": self._last_trigger,
                    "errors": self.errors, "skipped_budget": self.skipped_budget,
                    "lessons_added": self.lessons_added, "last_note": self._note,
                    "last_note_ts": self._note_ts, "recent_applied": list(self._applied),
                    "note": ("strategy-layer research meta-loop: independent model proposes "
                             "exploit/avoid contexts + bounded knob nudges + lessons. Observe-only "
                             "unless auto_apply (whitelisted+bounded). PAPER ONLY.")}

    def to_state(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "errors": self.errors, "skipped_budget": self.skipped_budget,
                    "lessons_added": self.lessons_added, "note": self._note, "note_ts": self._note_ts,
                    "applied": list(self._applied), "event_runs": self.event_runs,
                    "triggers": dict(self.triggers), "last_run_ts": self._last_run_ts}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        with self._lock:
            self.calls = int(data.get("calls", 0) or 0)
            self.errors = int(data.get("errors", 0) or 0)
            self.skipped_budget = int(data.get("skipped_budget", 0) or 0)
            self.lessons_added = int(data.get("lessons_added", 0) or 0)
            self._note = data.get("note")
            self._note_ts = float(data.get("note_ts", 0.0) or 0.0)
            self._applied = list(data.get("applied") or [])
            self.event_runs = int(data.get("event_runs", 0) or 0)
            self.triggers = {k: int(v or 0) for k, v in (data.get("triggers") or {}).items()}
            self._last_run_ts = float(data.get("last_run_ts", 0.0) or 0.0)
