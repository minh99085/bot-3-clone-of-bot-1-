"""Grok/xAI event-risk regime overlay for the BTC pulse engine (ADVISORY, off hot path).

A slow background worker periodically asks Grok for the current BTC regime + imminent
event risk, and exposes a small state the fast loop reads as a GATE. It can only ever make
the bot MORE cautious — declare a ``blackout`` (skip opening new paper trades) or inflate the
volatility estimate (``vol_multiplier`` >= 1 -> more conservative digital probabilities). It
can NEVER trigger a trade, loosen a gate, or place an order. Fail-open: if the call fails, is
stale, or no key is configured, the overlay is neutral and the engine runs as pure quant.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("hte.pulse.overlay")

_XAI_URL = "https://api.x.ai/v1/chat/completions"
_PROMPT = (
    "You are a real-time crypto risk monitor for a bot trading Bitcoin 5-minute 'up or down' "
    "markets. Assess the CURRENT BTC regime and whether a high-impact event (major macro "
    "release like CPI/FOMC, a large liquidation cascade, exchange outage, or major crypto "
    "headline) is imminent within ~10 minutes. Respond with STRICT JSON only, no prose: "
    '{"regime":"calm|elevated|event_risk","vol_multiplier":<1.0-3.0>,'
    '"blackout":<true|false>,"reason":"<short>"}. blackout=true ONLY for a clearly imminent '
    "high-impact event."
)


def xai_key_present() -> bool:
    return bool((os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip())


def make_xai_assessor(*, model: str = "grok-4.3", timeout_s: float = 12.0):
    """Build a callable ``() -> dict|None`` that asks Grok for the regime. Returns None on any
    failure (fail-open). Never trades; a single read-only chat completion."""
    box: dict = {}

    def _assess() -> Optional[dict]:
        key = (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip()
        if not key:
            return None
        try:
            import httpx
            c = box.get("c")
            if c is None:
                c = httpx.Client(timeout=timeout_s)
                box["c"] = c
            r = c.post(_XAI_URL,
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": model, "temperature": 0,
                             "messages": [{"role": "user", "content": _PROMPT}]})
            if r.status_code != 200:
                return None
            content = (((r.json() or {}).get("choices") or [{}])[0]
                       .get("message", {}).get("content", "") or "")
            content = content.strip().strip("`")
            if content.lower().startswith("json"):
                content = content[4:].strip()
            return json.loads(content)
        except Exception:  # noqa: BLE001 — overlay never raises into the loop
            return None
    return _assess


def _neutral() -> dict:
    return {"regime": "unknown", "vol_multiplier": 1.0, "blackout": False,
            "reason": "no overlay", "ts": 0.0}


def _sanitize(d: Optional[dict], now: float, *, vol_mult_cap: float) -> dict:
    if not isinstance(d, dict):
        return _neutral()
    try:
        vm = float(d.get("vol_multiplier", 1.0))
    except (TypeError, ValueError):
        vm = 1.0
    vm = max(1.0, min(float(vol_mult_cap), vm))      # only ever MORE cautious
    return {"regime": str(d.get("regime", "unknown"))[:32],
            "vol_multiplier": round(vm, 3),
            "blackout": bool(d.get("blackout", False)),
            "reason": str(d.get("reason", ""))[:200], "ts": now}


class GrokEventOverlay:
    def __init__(self, *, assessor=None, interval_s: float = 180.0,
                 max_calls_per_hour: int = 20, max_stale_s: float = 600.0,
                 vol_mult_cap: float = 3.0, budget=None):
        self._assessor = assessor if assessor is not None else make_xai_assessor()
        self._budget = budget          # optional shared GrokBudget (daily $ cap across consumers)
        self.interval_s = max(30.0, float(interval_s))
        self.max_calls_per_hour = int(max_calls_per_hour)
        self.max_stale_s = float(max_stale_s)
        self.vol_mult_cap = float(vol_mult_cap)
        self._lock = threading.Lock()
        self._state = _neutral()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._call_times: deque = deque()
        self.calls = 0
        self.errors = 0

    def _rate_ok(self, now: float) -> bool:
        while self._call_times and now - self._call_times[0] > 3600:
            self._call_times.popleft()
        return len(self._call_times) < self.max_calls_per_hour

    def refresh(self, now: Optional[float] = None) -> dict:
        """One assessment pass (used by the loop + directly in tests)."""
        now = float(now if now is not None else time.time())
        if not self._rate_ok(now):
            return self.current(now)
        # shared daily-cost guard (across overlay + analyst + predictor), if configured
        if self._budget is not None and not self._budget.try_spend("overlay", now=now):
            return self.current(now)
        self._call_times.append(now)
        raw = None
        try:
            raw = self._assessor() if self._assessor else None
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None:
            self.errors += 1
        else:
            self.calls += 1
        state = _sanitize(raw, now, vol_mult_cap=self.vol_mult_cap)
        with self._lock:
            # keep neutral ts=now so a failed call doesn't look "fresh-but-neutral" forever;
            # only a successful parse sets a non-neutral regime.
            self._state = state
        return state

    def current(self, now: Optional[float] = None) -> dict:
        """Latest overlay, or neutral if stale/unset (fail-open)."""
        now = float(now if now is not None else time.time())
        with self._lock:
            st = dict(self._state)
        if not st.get("ts") or (now - float(st["ts"])) > self.max_stale_s:
            return _neutral()
        return st

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                logger.debug("overlay refresh failed", exc_info=True)
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="grok-event-overlay",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"enabled": True, "interval_s": self.interval_s,
                "running": bool(self._thread is not None and self._thread.is_alive()),
                "calls": self.calls, "errors": self.errors, "state": self.current()}
