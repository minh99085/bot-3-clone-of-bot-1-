"""Grok/xAI signal-intelligence layer for the BTC 5-min pulse (OBSERVE-ONLY, off the hot path).

Two consumers, both advisory and graded-before-trusted:

* **A — GrokSignalAnalyst**: a periodic batch analyst that reads the TradingView signal-learning
  report and writes a research note (which RSI-divergence contexts are working/failing + why,
  overfitting warnings). Diagnostics only.
* **B — GrokSignalPredictor**: for each TradingView alert, asks Grok (asynchronously, in a
  background worker) for ``P(up)`` over the next 5 minutes. The engine reads the cached answer
  FAIL-OPEN (never blocks on Grok), records it as an observe-only feature, and SCORES it against
  the realized BTC move (leakage-free: the prediction is made before the outcome).

A shared **GrokBudget** caps daily cost + per-feature hourly calls across A, B, and the existing
event-risk overlay. Everything here is PAPER ONLY: nothing can place, size, or bypass a trade —
the strategy + execution gate remain the sole trade authority, and Grok output is recorded/graded,
never executed. Fail-open everywhere: bad/slow/no-key → neutral, engine runs as pure quant.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("hte.pulse.grok_intel")

_XAI_URL = "https://api.x.ai/v1/chat/completions"
_XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"


def xai_key() -> str:
    return (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip()


def _grok_chat(prompt: str, *, model: str, timeout_s: float, box: dict,
               extra_body: Optional[dict] = None) -> Optional[str]:
    """One read-only chat completion. Returns the content string or None (fail-open).

    ``extra_body`` is merged into the request JSON — used to enable xAI live search
    (``search_parameters``) so the decider can pull web/X news in parallel."""
    key = xai_key()
    if not key:
        return None
    try:
        import httpx
        c = box.get("c")
        if c is None:
            c = httpx.Client(timeout=timeout_s)
            box["c"] = c
        body = {"model": model, "temperature": 0,
                "messages": [{"role": "user", "content": prompt}]}
        if extra_body:
            body.update(extra_body)
        r = c.post(_XAI_URL, headers={"Authorization": f"Bearer {key}"}, json=body)
        if r.status_code != 200:
            return None
        return (((r.json() or {}).get("choices") or [{}])[0]
                .get("message", {}).get("content", "") or "")
    except Exception:  # noqa: BLE001 — never raise into the engine
        return None


def _grok_responses(prompt: str, *, model: str, timeout_s: float, box: dict,
                    tools: Optional[list] = None) -> Optional[str]:
    """One call to the xAI Agent Tools API (/v1/responses) with built-in server-side tools (e.g.
    web_search, x_search). Returns the assistant message text or None (fail-open)."""
    key = xai_key()
    if not key:
        return None
    try:
        import httpx
        c = box.get("rc")
        if c is None:
            c = httpx.Client(timeout=timeout_s)
            box["rc"] = c
        body = {"model": model, "input": [{"role": "user", "content": prompt}]}
        if tools:
            body["tools"] = tools
        r = c.post(_XAI_RESPONSES_URL, headers={"Authorization": f"Bearer {key}"}, json=body)
        if r.status_code != 200:
            return None
        j = r.json() or {}
        txt = j.get("output_text")
        if txt:
            return txt
        for it in (j.get("output") or []):
            if it.get("type") == "message":
                for part in (it.get("content") or []):
                    if part.get("text"):
                        return part["text"]
        return None
    except Exception:  # noqa: BLE001 — never raise into the engine
        return None


def _parse_json(content: Optional[str]) -> Optional[dict]:
    """Robustly extract a JSON object from an LLM reply: handles ```json fences, leading labels,
    and prose wrapped around the object (falls back to the first balanced {...} block)."""
    if not content:
        return None
    s = content.strip()
    # strip ```json ... ``` (or bare ```) fences
    if "```" in s:
        import re as _re
        m = _re.search(r"```(?:json)?\s*(.*?)```", s, _re.DOTALL | _re.IGNORECASE)
        if m:
            s = m.group(1).strip()
    s = s.strip().strip("`").strip()
    if s.lower().startswith("json"):
        s = s[4:].strip()
    try:
        d = json.loads(s)
        if isinstance(d, dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    # fallback: scan for the first balanced top-level {...} object (ignores surrounding prose)
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    d = json.loads(s[start:i + 1])
                    return d if isinstance(d, dict) else None
                except Exception:  # noqa: BLE001
                    return None
    return None


# --------------------------------- shared budget guard ------------------------------------- #
class GrokBudget:
    """Thread-safe shared cap across all Grok consumers: a daily USD ceiling plus per-feature
    hourly call limits. ``try_spend(feature)`` returns False when a limit is hit (caller skips)."""

    def __init__(self, *, daily_usd_cap: float = 5.0, est_usd_per_call: float = 0.02,
                 per_feature_hourly: Optional[dict] = None):
        self.daily_usd_cap = float(daily_usd_cap)
        self.est_usd_per_call = float(est_usd_per_call)
        self.per_feature_hourly = dict(per_feature_hourly or {})
        self._lock = threading.Lock()
        self._day = None
        self._spent_today = 0.0
        self._calls_today = 0
        self._feature_times: dict = {}     # feature -> deque[ts]

    def try_spend(self, feature: str, *, now: Optional[float] = None) -> bool:
        now = float(now if now is not None else time.time())
        with self._lock:
            day = int(now // 86400)
            if day != self._day:
                self._day, self._spent_today, self._calls_today = day, 0.0, 0
                self._feature_times = {}
            if self._spent_today + self.est_usd_per_call > self.daily_usd_cap:
                return False
            cap = self.per_feature_hourly.get(feature)
            if cap is not None:
                dq = self._feature_times.setdefault(feature, deque())
                while dq and now - dq[0] > 3600:
                    dq.popleft()
                if len(dq) >= int(cap):
                    return False
                dq.append(now)
            self._spent_today += self.est_usd_per_call
            self._calls_today += 1
            return True

    def status(self) -> dict:
        with self._lock:
            return {"daily_usd_cap": self.daily_usd_cap,
                    "est_usd_per_call": self.est_usd_per_call,
                    "spent_today_usd": round(self._spent_today, 4),
                    "calls_today": self._calls_today,
                    "per_feature_hourly": dict(self.per_feature_hourly)}


# --------------------------------- B: per-signal predictor --------------------------------- #
def make_signal_predictor(*, model: str = "grok-4.3", timeout_s: float = 12.0):
    """Build ``predictor_fn(context) -> float|None`` (P(up) in [0,1]) using Grok. Fail-open."""
    box: dict = {}

    def _predict(context: dict) -> Optional[float]:
        prompt = (
            "You are a quant assistant scoring a short-horizon Bitcoin signal. Given a TradingView "
            "indicator alert and recent BTC context, estimate the probability that BTC's Chainlink "
            "price will be HIGHER 5 minutes from now. Be calibrated and conservative; 0.5 means no "
            "edge. Respond with STRICT JSON only: {\"p_up\":<0.0-1.0>,\"reason\":\"<short>\"}.\n"
            "SIGNAL+CONTEXT: " + json.dumps(context, default=str)[:2000])
        d = _parse_json(_grok_chat(prompt, model=model, timeout_s=timeout_s, box=box))
        if not d or d.get("p_up") is None:
            return None
        try:
            return max(0.0, min(1.0, float(d["p_up"])))
        except (TypeError, ValueError):
            return None
    return _predict


class GrokSignalPredictor:
    """B: observe-only per-signal P(up). Background worker; engine reads cached results fail-open.
    Predictions are scored against the realized BTC move (leakage-free). Never trades."""

    def __init__(self, *, predictor_fn=None, budget: Optional[GrokBudget] = None,
                 max_pending: int = 200, max_results: int = 5000):
        self._fn = predictor_fn if predictor_fn is not None else make_signal_predictor()
        self._budget = budget
        self._lock = threading.Lock()
        self._queue: deque = deque(maxlen=int(max_pending))
        self._results: dict = {}          # event_id -> {"p_up", "ts"}
        self._result_order: deque = deque(maxlen=int(max_results))
        self._seen: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.requested = 0
        self.predicted = 0
        self.errors = 0
        self.skipped_budget = 0
        # scoring (vs realized BTC move) — leakage-free
        self.scored = 0
        self.correct = 0
        self.brier_sum = 0.0

    def request(self, event_id: str, context: dict) -> None:
        if not event_id:
            return
        with self._lock:
            if event_id in self._seen:
                return
            self._seen.add(event_id)
            self._queue.append((event_id, context))
            self.requested += 1

    def get(self, event_id: str) -> Optional[dict]:
        with self._lock:
            r = self._results.get(event_id)
            return dict(r) if r else None

    def _process_one(self) -> bool:
        with self._lock:
            if not self._queue:
                return False
            event_id, context = self._queue.popleft()
        if self._budget is not None and not self._budget.try_spend("predictor"):
            with self._lock:
                self.skipped_budget += 1
            return True
        p_up = None
        try:
            p_up = self._fn(context)
        except Exception:  # noqa: BLE001
            p_up = None
        with self._lock:
            if p_up is None:
                self.errors += 1
            else:
                self.predicted += 1
                self._results[event_id] = {"p_up": round(float(p_up), 4), "ts": time.time()}
                self._result_order.append(event_id)
                if len(self._results) > self._result_order.maxlen:
                    old = self._result_order.popleft()
                    self._results.pop(old, None)
        return True

    def score(self, event_id: str, outcome_up: bool) -> None:
        """Score a prior prediction against the realized move (call once per event at horizon)."""
        with self._lock:
            r = self._results.get(event_id)
            if not r:
                return
            p = float(r["p_up"])
            self.scored += 1
            self.correct += int((p > 0.5) == bool(outcome_up))
            self.brier_sum += (p - (1.0 if outcome_up else 0.0)) ** 2

    def _worker(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                worked = self._process_one()
            except Exception:  # noqa: BLE001
                logger.debug("predictor worker error", exc_info=True)
            self._stop.wait(0.2 if worked else 1.0)

    def start(self) -> "GrokSignalPredictor":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="grok-signal-predictor",
                                            daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def report(self) -> dict:
        with self._lock:
            acc = round(self.correct / self.scored, 4) if self.scored else None
            brier = round(self.brier_sum / self.scored, 4) if self.scored else None
            return {"enabled": True, "observe_only": True, "affects_trading": False,
                    "off_hot_path": True, "requested": self.requested,
                    "predicted": self.predicted, "errors": self.errors,
                    "skipped_budget": self.skipped_budget, "scored": self.scored,
                    "accuracy": acc, "brier": brier, "pending": len(self._queue),
                    "note": ("observe-only Grok P(up) per signal; graded vs realized BTC move "
                             "before it could ever be trusted; never places/sizes/bypasses a trade.")}

    def to_state(self) -> dict:
        with self._lock:
            return {"requested": self.requested, "predicted": self.predicted,
                    "errors": self.errors, "skipped_budget": self.skipped_budget,
                    "scored": self.scored, "correct": self.correct,
                    "brier_sum": round(self.brier_sum, 6)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        with self._lock:
            self.requested = int(data.get("requested", 0) or 0)
            self.predicted = int(data.get("predicted", 0) or 0)
            self.errors = int(data.get("errors", 0) or 0)
            self.skipped_budget = int(data.get("skipped_budget", 0) or 0)
            self.scored = int(data.get("scored", 0) or 0)
            self.correct = int(data.get("correct", 0) or 0)
            self.brier_sum = float(data.get("brier_sum", 0.0) or 0.0)


# --------------------------------- A: batch analyst ---------------------------------------- #
def make_signal_analyst(*, model: str = "grok-4.3", timeout_s: float = 30.0):
    """Build ``analyst_fn(report) -> dict|None`` that LEARNS the bot's trading patterns over time.

    The report now carries the bot's GROWING learned evidence — settled-trade bucket performance
    (pnl_by_*), the learned-selectivity bucket evidence (with breakeven / confidence), the
    late-window time-decay edge, gate stats, edge-model calibration, and the TradingView signal
    learning — PLUS the analyst's own ``prior_analysis``. The prompt asks Grok to refine its prior
    understanding as the sample grows (continuity), so it scrubs the data better the more the bot
    learns. Diagnostics only — never recommends going live or placing a trade."""
    box: dict = {}

    def _analyze(report: dict) -> Optional[dict]:
        prompt = (
            "You are a quant researcher CONTINUOUSLY LEARNING an OBSERVE-ONLY paper-trading bot's "
            "BTC 5-minute trading patterns. You are given the bot's GROWING learned evidence: "
            "settled-trade performance bucketed by regime/z-score/time-to-close/conviction/entry-"
            "mode (pnl_by_*), the learned-selectivity bucket evidence (each bucket's win-rate vs its "
            "OWN breakeven win-rate, with a confidence flag), the late-window time-decay edge "
            "(cohort vs other), gate stats, edge-model calibration, and TradingView signal learning. "
            "You are ALSO given your own PRIOR analysis ('prior_analysis') and how many analyses you "
            "have done ('analyses_done'). REFINE your understanding as the sample grows: which "
            "patterns are now CONFIRMED profitable (win-rate confidently above breakeven, positive "
            "EV after cost, enough samples) vs which are noise/overfit; what CHANGED since your prior "
            "analysis; and where the bot should focus next. Be calibrated; ignore buckets with n<8. "
            "Do NOT recommend going live. Respond with STRICT JSON only: {\"summary\":\"<2-3 "
            "sentences>\",\"working\":[\"...\"],\"failing\":[\"...\"],\"warnings\":[\"...\"],"
            "\"changes_since_last\":[\"...\"],\"focus_next\":[\"...\"]}.\nEVIDENCE: "
            + json.dumps(report, default=str)[:9000])
        d = _parse_json(_grok_chat(prompt, model=model, timeout_s=timeout_s, box=box))
        if not d:
            return None
        return {"summary": str(d.get("summary", ""))[:1000],
                "working": [str(x)[:200] for x in (d.get("working") or [])][:8],
                "failing": [str(x)[:200] for x in (d.get("failing") or [])][:8],
                "warnings": [str(x)[:200] for x in (d.get("warnings") or [])][:8],
                "changes_since_last": [str(x)[:200] for x in (d.get("changes_since_last") or [])][:6],
                "focus_next": [str(x)[:200] for x in (d.get("focus_next") or [])][:6]}
    return _analyze


class GrokSignalAnalyst:
    """A: periodic observe-only batch analysis of the signal-learning report. Diagnostics only."""

    def __init__(self, *, analyst_fn=None, budget: Optional[GrokBudget] = None,
                 interval_s: float = 1800.0, report_provider=None):
        self._fn = analyst_fn if analyst_fn is not None else make_signal_analyst()
        self._budget = budget
        self.interval_s = max(120.0, float(interval_s))
        self._report_provider = report_provider     # callable -> dict (the report to analyze)
        self._lock = threading.Lock()
        self._note: Optional[dict] = None
        self._note_ts = 0.0
        self._history: deque = deque(maxlen=20)      # rolling memory of past notes (Grok "learning")
        self.calls = 0
        self.errors = 0
        self.skipped_budget = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def refresh(self) -> Optional[dict]:
        if self._budget is not None and not self._budget.try_spend("analyst"):
            self.skipped_budget += 1
            return None
        report = {}
        try:
            report = self._report_provider() if self._report_provider else {}
        except Exception:  # noqa: BLE001
            report = {}
        # continuity: give Grok its prior analysis + how many it has done, so it REFINES (learns)
        # its understanding as the bot's evidence grows rather than starting fresh each cycle.
        with self._lock:
            report["prior_analysis"] = self._note
            report["analyses_done"] = self.calls
        note = None
        try:
            note = self._fn(report)
        except Exception:  # noqa: BLE001
            note = None
        if note is None:
            self.errors += 1
        else:
            self.calls += 1
            with self._lock:
                self._note, self._note_ts = note, time.time()
                self._history.append({"ts": round(self._note_ts, 1),
                                      "summary": note.get("summary", ""),
                                      "changes_since_last": note.get("changes_since_last", [])})
        return note

    def _worker(self) -> None:
        # small initial delay so there is some data to analyze
        self._stop.wait(min(self.interval_s, 60.0))
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                logger.debug("analyst worker error", exc_info=True)
            self._stop.wait(self.interval_s)

    def start(self) -> "GrokSignalAnalyst":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="grok-signal-analyst",
                                            daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def report(self) -> dict:
        with self._lock:
            note = dict(self._note) if self._note else None
            ts = self._note_ts
            history = list(self._history)
        return {"enabled": True, "observe_only": True, "affects_trading": False,
                "interval_s": self.interval_s, "calls": self.calls, "errors": self.errors,
                "skipped_budget": self.skipped_budget, "last_note": note, "last_note_ts": ts,
                "learns_from": "bot_growing_evidence_with_continuity",
                "history": history[-8:],
                "note": ("observe-only Grok research analyst that LEARNS the bot's trading patterns "
                         "from its growing settled-trade/selectivity/late-window evidence and refines "
                         "across cycles (continuity). Diagnostics only — never trades.")}

    def to_state(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "errors": self.errors,
                    "skipped_budget": self.skipped_budget,
                    "note": self._note, "note_ts": self._note_ts,
                    "history": list(self._history)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        with self._lock:
            self.calls = int(data.get("calls", 0) or 0)
            self.errors = int(data.get("errors", 0) or 0)
            self.skipped_budget = int(data.get("skipped_budget", 0) or 0)
            self._note = data.get("note")
            self._note_ts = float(data.get("note_ts", 0.0) or 0.0)
            self._history = deque((data.get("history") or []), maxlen=20)
