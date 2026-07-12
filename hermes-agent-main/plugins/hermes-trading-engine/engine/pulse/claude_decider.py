"""Claude directional second-opinion for the LLM council (PAPER ONLY; observe/advisory, fail-open).

A lightweight, INDEPENDENT ``p_up`` estimate from Claude per window -- the second LLM voice in the
council so both models' compute drives the decision (Grok is the other). Different model + different
instructions than Grok = catches different errors. Runs on a background worker (never blocks the
tick), budget-gated, and fail-open (returns None on any error). It only emits a probability; the
council blends it and the deterministic execution floor still decides whether a paper fill happens.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

from engine.pulse.grok_intel import _parse_json
from engine.pulse.grok_bundle import serialize_bundle_for_grok
from engine.pulse.claude_client import claude_chat


def _clamp01(v) -> Optional[float]:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


def make_claude_decider_fn(*, model: Optional[str] = None, timeout_s: float = 18.0,
                           chat=claude_chat):
    """Build ``fn(bundle) -> {"p_up","confidence"}|None``. Fail-open (None on any error)."""
    box: dict = {}
    system = ("You are an INDEPENDENT probabilistic forecaster for a PAPER Polymarket BTC up/down "
              "window (settles UP if Chainlink BTC/USD close >= open; ties -> UP). Estimate the "
              "probability the window closes UP from the evidence. Be calibrated and skeptical; you "
              "are ONE voice in an ensemble, not the sole decider. When tradingview_alert_history "
              "is present, read the last 10 alerts per symbol (oldest→newest) and their trend.pattern "
              "to trace up/down price-movement trends — observe-only context, not a trade gate.")

    def _decide(bundle: dict) -> Optional[dict]:
        prompt = ("Estimate P(up) for this window. Respond STRICT JSON ONLY: "
                  "{\"p_up\":<0-1>,\"confidence\":<0-1>,\"rationale\":\"<short>\"}.\n"
                  "BUNDLE: " + serialize_bundle_for_grok(bundle))
        d = _parse_json(chat(prompt, model=model, timeout_s=timeout_s, box=box, system=system,
                             max_tokens=400))
        if not isinstance(d, dict):
            return None
        pu = _clamp01(d.get("p_up"))
        if pu is None:
            return None
        return {"p_up": round(pu, 4), "confidence": round(_clamp01(d.get("confidence")) or 0.0, 4)}
    return _decide


class ClaudeDecider:
    """Background Claude ``p_up`` worker: engine ``request``s per window and reads cached ``get``.
    PAPER ONLY, fail-open, budget-gated."""

    def __init__(self, *, decider_fn=None, budget=None, ttl_s: float = 240.0,
                 max_pending: int = 200, max_results: int = 5000):
        self._fn = decider_fn if decider_fn is not None else make_claude_decider_fn()
        self._budget = budget
        self.ttl_s = float(ttl_s)
        self._lock = threading.Lock()
        self._queue: deque = deque(maxlen=int(max_pending))
        self._results: dict = {}
        self._order: deque = deque(maxlen=int(max_results))
        self._seen: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.requested = 0
        self.decided = 0
        self.errors = 0
        self.skipped_budget = 0
        self.latency_sum = 0.0

    def request(self, decision_id: str, bundle: dict, *, refresh_token: Optional[str] = None) -> None:
        if not decision_id:
            return
        key = decision_id if not refresh_token else "%s#%s" % (decision_id, refresh_token)
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
            self._queue.append((decision_id, bundle))
            self.requested += 1

    def get(self, decision_id: str) -> Optional[dict]:
        with self._lock:
            r = self._results.get(decision_id)
            return dict(r) if r else None

    def _process_one(self) -> bool:
        with self._lock:
            if not self._queue:
                return False
            decision_id, bundle = self._queue.popleft()
        if self._budget is not None and not self._budget.try_spend("claude_decider"):
            with self._lock:
                self.skipped_budget += 1
            return True
        t0 = time.time()
        dec = None
        try:
            dec = self._fn(bundle)
        except Exception:  # noqa: BLE001 — fail-open
            dec = None
        with self._lock:
            if dec is None:
                self.errors += 1
            else:
                dec["ts"] = time.time()
                self.decided += 1
                self.latency_sum += time.time() - t0
                self._results[decision_id] = dec
                self._order.append(decision_id)
                if len(self._results) > self._order.maxlen:
                    self._results.pop(self._order.popleft(), None)
        return True

    def _worker(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                worked = self._process_one()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(0.2 if worked else 1.0)

    def start(self) -> "ClaudeDecider":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="claude-decider", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def report(self) -> dict:
        with self._lock:
            return {"enabled": True, "paper_only": True, "requested": self.requested,
                    "decided": self.decided, "errors": self.errors,
                    "skipped_budget": self.skipped_budget, "pending": len(self._queue),
                    "avg_latency_s": (round(self.latency_sum / self.decided, 3)
                                      if self.decided else None)}
