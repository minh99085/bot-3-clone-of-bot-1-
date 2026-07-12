"""Grok Decision Engine for the BTC 5-min pulse — the "Grok decides, bot executes" architecture.

This inverts the usual hierarchy: instead of the quant engine deciding and Grok observing, Grok is
asked to synthesize EVERYTHING the bot knows (TradingView signals incl. order-flow/event fields,
market microstructure, price/vol regime, the bot's own LEARNED evidence, position/account state,
and — optionally — live web/X news) and return a structured TRADE DECISION.

Two safety properties are non-negotiable and built in here:

* **PAPER ONLY.** Nothing in this module places a real order; it only emits an advisory decision
  the engine may act on in paper mode.
* **Fail-CLOSED.** A decider that times out, blows its budget, returns malformed output, or is below
  the confidence floor yields ``no_trade`` (abstain) — it never produces a blind trade.

Modes (engine-controlled): ``off`` (disabled), ``shadow`` (decide + grade every window but DO NOT
trade — the safe default). ``follow`` is removed; if requested it is coerced to shadow.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Optional

from engine.pulse.grok_bundle import serialize_bundle_for_grok
from engine.pulse.grok_intel import _grok_chat, _grok_responses, _parse_json, GrokBudget

logger = logging.getLogger("hte.pulse.grok_decider")

ACTIONS = ("up", "down", "no_trade")
_ACTION_ALIASES = {
    "up": "up", "buy": "up", "long": "up", "bull": "up", "bullish": "up", "yes": "up",
    "down": "down", "sell": "down", "short": "down", "bear": "down", "bearish": "down", "no": "down",
    "no_trade": "no_trade", "no-trade": "no_trade", "hold": "no_trade", "flat": "no_trade",
    "none": "no_trade", "skip": "no_trade", "abstain": "no_trade", "wait": "no_trade",
}


def _wilson_lower(correct: int, n: int, z: float = 1.64) -> Optional[float]:
    """One-sided lower bound of the Wilson interval — used to flag a directional edge only when we
    are statistically confident accuracy is above 0.5 (not just a small-sample fluke)."""
    if n <= 0:
        return None
    import math
    phat = correct / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return max(0.0, center - margin)


def _wilson_upper(correct: int, n: int, z: float = 1.64) -> Optional[float]:
    """One-sided upper Wilson bound — used to flag a context as proven-LOSING (upper < 0.5)."""
    if n <= 0:
        return None
    import math
    phat = correct / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return min(1.0, center + margin)


class AggressionController:
    """Self-tuning aggression for the adaptive loop: LOOSEN as acted trades turn profitable, TIGHTEN
    when they lose — and repeat. ``aggression`` in [min,max] scales exploration rate, the exploit
    confidence bar (looser when winning), and position size. Asymmetric (steps down faster than up)
    so it backs off losses quickly. The hard circuit breaker remains the floor under this."""

    def __init__(self, *, min_aggr: float = 0.0, max_aggr: float = 1.0, start: float = 0.0,
                 step_up: float = 0.05, step_down: float = 0.1, eval_window: int = 12):
        self.min = float(min_aggr)
        self.max = float(max_aggr)
        self.aggression = float(start)
        self.step_up = float(step_up)
        self.step_down = float(step_down)
        self._recent: deque = deque(maxlen=int(eval_window))
        self.updates = 0

    def record(self, pnl: float) -> None:
        """Feed one settled ACTED-trade PnL; ratchet aggression on the recent realized net."""
        self._recent.append(float(pnl or 0.0))
        self.updates += 1
        if len(self._recent) < max(3, self._recent.maxlen // 2):
            return                                  # need a little data before adjusting
        net = sum(self._recent)
        if net > 0:
            self.aggression = min(self.max, self.aggression + self.step_up)   # winning -> loosen
        elif net < 0:
            self.aggression = max(self.min, self.aggression - self.step_down)  # losing -> tighten

    def effective_explore_rate(self, base: float) -> float:
        """More exploration when winning (up to ~0.95), never below the configured base."""
        return max(float(base), min(0.95, float(base) + self.aggression * (0.95 - float(base))))

    def exploit_margin(self, base: float) -> float:
        """Lower the exploit confidence bar as aggression rises (looser, bounded so it can dip a
        little below 0.5 only when consistently winning)."""
        return max(-0.05, float(base) - self.aggression * 0.10)

    def size_scale(self) -> float:
        """Scale position size up to 2x with aggression."""
        return 1.0 + self.aggression

    def status(self) -> dict:
        return {"aggression": round(self.aggression, 4), "min": self.min, "max": self.max,
                "step_up": self.step_up, "step_down": self.step_down,
                "recent_net_pnl": round(sum(self._recent), 4), "updates": self.updates,
                "note": "loosens (more explore/looser exploit/larger size) as acted trades profit; "
                        "tightens on losses; circuit breaker is the hard floor."}

    def to_state(self) -> dict:
        return {"aggression": self.aggression, "updates": self.updates, "recent": list(self._recent)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.aggression = max(self.min, min(self.max, float(data.get("aggression", self.aggression))))
        self.updates = int(data.get("updates", 0) or 0)
        self._recent = deque((data.get("recent") or []), maxlen=self._recent.maxlen)


def _clamp01(v, default: Optional[float] = None) -> Optional[float]:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def normalize_decision(d, *, default_ttl_s: float = 240.0) -> Optional[dict]:
    """Parse + validate Grok's raw output into a canonical decision, or None (fail-closed)."""
    if not isinstance(d, dict):
        return None
    raw_action = str(d.get("action") or d.get("decision") or d.get("side") or "").strip().lower()
    action = _ACTION_ALIASES.get(raw_action)
    if action is None:
        return None                                   # unknown action -> caller treats as no decision
    conf = _clamp01(d.get("confidence"), 0.0)
    size = _clamp01(d.get("size_fraction"), 1.0 if action != "no_trade" else 0.0)
    if action == "no_trade":
        size = 0.0
    # p_up: Grok's probability BTC closes UP — REQUIRED on every window (even no_trade) so the
    # directional VIEW is graded each window and Grok accumulates calibrated edge data fast.
    p_up = _clamp01(d.get("p_up"))
    if p_up is None:
        c = conf or 0.5
        p_up = c if action == "up" else ((1.0 - c) if action == "down" else 0.5)
    mp = None
    try:
        if d.get("max_price") is not None:
            mp = max(0.0, min(1.0, float(d.get("max_price"))))
    except (TypeError, ValueError):
        mp = None
    try:
        ttl = float(d.get("ttl_s")) if d.get("ttl_s") is not None else float(default_ttl_s)
    except (TypeError, ValueError):
        ttl = float(default_ttl_s)
    out = {"action": action, "confidence": round(conf or 0.0, 4), "p_up": round(p_up, 4),
           "size_fraction": round(size or 0.0, 4), "max_price": mp,
           "ttl_s": max(0.0, ttl),
           "key_risks": [str(x)[:160] for x in (d.get("key_risks") or [])][:6],
           "rationale": str(d.get("rationale") or "")[:600],
           "schema_version": str(d.get("schema_version") or "2")}
    for opt_key in ("tv_read", "trend_read", "mispricing_read", "edge_quality"):
        if d.get(opt_key) is not None:
            out[opt_key] = str(d.get(opt_key))[:400]
    return out


def build_decider_prompt(bundle: dict, *, use_search: bool = False) -> str:
    """Instructions + ordered bundle JSON for the Grok decider (v2)."""
    return (
        "You are the lead decision-maker for an OBSERVE-AND-PAPER-TRADE Polymarket BTC bot. "
        "Markets settle UP if Chainlink BTC/USD at window close >= open (ties -> UP). "
        "You receive 5m AND 15m windows; read grok_task.primary_series and timing.seconds_to_close "
        "for THIS decision. "
        "DECISION FRAMEWORK (do this mentally before answering):\n"
        "1) MISPRICING: read cex_lead_mispricing — divergence=cex_p_up-poly_yes is the primary edge; "
        "note confirmed/late_decisive (ignore tv_confirms when price_action_trend is present).\n"
        "2) SPOT TREND: read price_action_trend — use rising/falling/flat from BTC & ETH Chainlink "
        "spot vs window open (move_from_open_bps). Do NOT use TradingView UP/DOWN labels for trend; "
        "tradingview_trend is observe-only context when present.\n"
        "3) LEARNED TV: tv_signal_learning shows historical signal_level buckets (observe-only).\n"
        "4) PAYOFF: polymarket asks + payoff breakeven — only act up/down if p_up clears breakeven "
        "after costs AND mispricing+spot trend align (rising→up, falling→down).\n"
        "5) SELF-CALIBRATION: decider_track_record + trade_decision_history — avoid repeating "
        "losing contexts; exploit proven buckets.\n"
        "model_vs_market: trust market price + CEX lead over raw edge_model_p_up. "
        "For 15m: when grok_task.in_entry_band is true, weight fresh price_action_trend heavily. "
        "Prefer no_trade when uncertain; ALWAYS output p_up (graded every window). "
        + ("You may use live web/X news in the bundle. " if use_search else "")
        + "Respond STRICT JSON ONLY:\n"
        '{"action":"up|down|no_trade","p_up":<0-1>,"confidence":<0-1>,'
        '"size_fraction":<0-1>,"max_price":<0-1 optional>,'
        '"trend_read":"<1-2 sentences: BTC/ETH rising/falling/flat from spot prices>",'
        '"mispricing_read":"<1 sentence: divergence side + confirmed?>",'
        '"edge_quality":"none|weak|medium|strong",'
        '"key_risks":["..."],"rationale":"<short>","ttl_s":<seconds>}\n'
        "BUNDLE: " + serialize_bundle_for_grok(bundle))


def build_light_decider_prompt(bundle: dict) -> str:
    """Cheap tier: calibrate p_up + edge_quality only (graded every window)."""
    return (
        "Calibrate a Polymarket BTC up/down window. Settles UP if Chainlink close >= open. "
        "LIGHT tier — estimate p_up only; action should usually be no_trade unless edge is obvious. "
        "Read: timing, price move, digital_fair_p_up, polymarket yes_mid, cex_lead_mispricing, "
        "price_action_trend (BTC/ETH rising/falling/flat from spot — NOT TV UP/DOWN). "
        "Respond STRICT JSON ONLY: "
        '{"action":"up|down|no_trade","p_up":<0-1>,"confidence":<0-1>,'
        '"size_fraction":0,"edge_quality":"none|weak|medium|strong",'
        '"trend_read":"<brief spot trend>","mispricing_read":"<brief>","rationale":"<brief>","ttl_s":240}\n'
        "BUNDLE: " + serialize_bundle_for_grok(bundle))


def make_decider_fn(*, model: str = "grok-4.3", timeout_s: float = 12.0,
                    use_search: bool = False, use_search_deep_only: bool = True,
                    default_ttl_s: float = 240.0, chat=_grok_chat):
    """Build ``decider_fn(bundle) -> decision|None``. ``use_search`` enables xAI live web/X search
    so Grok can pull fresh BTC news/sentiment in parallel. Fail-open (returns None on any error)."""
    box: dict = {}
    extra = None
    if use_search:
        extra = {"search_parameters": {"mode": "auto",
                                       "sources": [{"type": "web"}, {"type": "x"}],
                                       "max_search_results": 8}}

    def _decide(bundle: dict) -> Optional[dict]:
        tier = str(bundle.get("grok_compute_tier") or "full").lower()
        if tier == "light":
            prompt = build_light_decider_prompt(bundle)
            extra_body = None
        else:
            deep_search = use_search and (tier == "deep" if use_search_deep_only else True)
            prompt = build_decider_prompt(bundle, use_search=deep_search)
            extra_body = extra if deep_search else None
        content = chat(prompt, model=model, timeout_s=timeout_s, box=box, extra_body=extra_body)
        return normalize_decision(_parse_json(content), default_ttl_s=default_ttl_s)
    return _decide


def make_news_fn(*, model: str = "grok-4.3", timeout_s: float = 35.0, responses=_grok_responses):
    """Build ``news_fn() -> digest|None`` that pulls a short BTC news/sentiment digest via the xAI
    Agent Tools API (built-in web_search + x_search on /v1/responses). Separated from the per-window
    decision so news is gathered periodically (cheap, bounded) and injected into every bundle."""
    box: dict = {}
    tools = [{"type": "web_search"}, {"type": "x_search"}]

    def _news() -> Optional[dict]:
        prompt = (
            "Search the latest web + X for BREAKING Bitcoin news and sentiment in the last ~30 "
            "minutes that could move BTC over the NEXT 5 MINUTES (macro prints, ETF flows, "
            "exchange/regulatory headlines, large liquidations, prominent X posts). Summarize for a "
            "short-horizon trader. Reply with STRICT JSON only: {\"sentiment\":\"bullish|bearish|"
            "neutral\",\"confidence\":<0-1>,\"headlines\":[\"...\"],\"event_risk\":\"low|medium|"
            "high\"}.")
        d = _parse_json(responses(prompt, model=model, timeout_s=timeout_s, box=box, tools=tools))
        if not d:
            return None
        return {"sentiment": str(d.get("sentiment", "neutral"))[:20],
                "confidence": _clamp01(d.get("confidence"), 0.0),
                "headlines": [str(x)[:200] for x in (d.get("headlines") or [])][:6],
                "event_risk": str(d.get("event_risk", "low"))[:12]}
    return _news


class GrokNewsDigest:
    """Periodic BTC news/sentiment digest (xAI live search), cached + injected into every decision
    bundle. Budget-gated + fail-open. Observe-only context; the decision still belongs to Grok."""

    def __init__(self, *, news_fn=None, budget: Optional[GrokBudget] = None,
                 interval_s: float = 300.0, max_age_s: float = 600.0):
        self._fn = news_fn if news_fn is not None else make_news_fn()
        self._budget = budget
        self.interval_s = max(60.0, float(interval_s))
        self.max_age_s = float(max_age_s)
        self._lock = threading.Lock()
        self._digest: Optional[dict] = None
        self._ts = 0.0
        self.calls = 0
        self.errors = 0
        self.skipped_budget = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def refresh(self) -> Optional[dict]:
        if self._budget is not None and not self._budget.try_spend("news"):
            self.skipped_budget += 1
            return None
        d = None
        try:
            d = self._fn()
        except Exception:  # noqa: BLE001
            d = None
        if d is None:
            self.errors += 1
        else:
            self.calls += 1
            with self._lock:
                self._digest, self._ts = d, time.time()
        return d

    def latest(self) -> Optional[dict]:
        with self._lock:
            if not self._digest or (time.time() - self._ts) > self.max_age_s:
                return None
            return {**self._digest, "age_s": round(time.time() - self._ts, 1)}

    def _worker(self) -> None:
        self._stop.wait(min(self.interval_s, 15.0))
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.interval_s)

    def start(self) -> "GrokNewsDigest":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="grok-news-digest",
                                            daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def report(self) -> dict:
        with self._lock:
            return {"enabled": True, "interval_s": self.interval_s, "calls": self.calls,
                    "errors": self.errors, "skipped_budget": self.skipped_budget,
                    "latest": (dict(self._digest) if self._digest else None),
                    "age_s": (round(time.time() - self._ts, 1) if self._digest else None)}

    def to_state(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "errors": self.errors,
                    "skipped_budget": self.skipped_budget, "digest": self._digest, "ts": self._ts}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        with self._lock:
            self.calls = int(data.get("calls", 0) or 0)
            self.errors = int(data.get("errors", 0) or 0)
            self.skipped_budget = int(data.get("skipped_budget", 0) or 0)
            self._digest = data.get("digest")
            self._ts = float(data.get("ts", 0.0) or 0.0)


class GrokDecider:
    """Background decision worker + grader. The engine ``request``s a decision per window, reads the
    cached ``get`` result fail-open, and ``grade``s it against the realized outcome. PAPER ONLY."""

    def __init__(self, *, decider_fn=None, budget: Optional[GrokBudget] = None,
                 mode: str = "shadow", min_confidence: float = 0.55, ttl_s: float = 240.0,
                 view_promote_min_samples: int = 25,
                 adaptive_min_samples: int = 20, adaptive_margin: float = 0.0,
                 max_pending: int = 200, max_results: int = 5000):
        self.view_promote_min_samples = int(view_promote_min_samples)
        self.adaptive_min_samples = int(adaptive_min_samples)
        self.adaptive_margin = float(adaptive_margin)
        self.aggr = AggressionController()          # self-tuning loosen-on-profit / tighten-on-loss
        self._fn = decider_fn if decider_fn is not None else make_decider_fn()
        self._budget = budget
        _mode = str(mode or "shadow").strip().lower()
        if _mode == "follow":
            logger.warning("grok_decider follow mode removed; using shadow (observe-only)")
            _mode = "shadow"
        self.mode = _mode if _mode in ("off", "shadow") else "off"
        self.min_confidence = float(min_confidence)
        self.ttl_s = float(ttl_s)
        self._lock = threading.Lock()
        self._queue: deque = deque(maxlen=int(max_pending))
        self._results: dict = {}              # decision_id -> decision (+ "ts","latency_s")
        self._order: deque = deque(maxlen=int(max_results))
        self._seen: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # counters
        self.requested = 0
        self.decided = 0
        self.errors = 0
        self.skipped_budget = 0
        self.latency_sum = 0.0
        # grading (direction vs realized 5-min outcome; abstains tracked separately)
        self.graded = 0
        self.correct = 0
        self.brier_sum = 0.0
        self.abstains = 0
        self.by_action: dict = {}             # action -> {"n","wins","pnl"}
        # ---- directional VIEW grading (p_up vs realized close on EVERY window, traded or not) ----
        # this is the rich, always-on edge data: Grok's p_up is scored each window so it accumulates
        # a calibrated track record even while it abstains from trading.
        self.view_graded = 0
        self.view_correct = 0
        self.view_brier_sum = 0.0
        # ---- learning-as-it-trades: per-context VIEW accuracy + recent graded outcomes ----
        self.by_context: dict = {}            # dim -> bucket -> {"n","correct"} (by p_up view)
        self._recent: deque = deque(maxlen=12)

    # -- request / read ----------------------------------------------------- #
    def request(self, decision_id: str, bundle: dict, context: Optional[dict] = None,
                *, refresh_token: Optional[str] = None) -> None:
        """Queue one Grok decision. ``refresh_token`` allows a new call when TV/entry-band updates."""
        if not decision_id or self.mode == "off":
            return
        key = decision_id if not refresh_token else "%s#%s" % (decision_id, refresh_token)
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
            self._queue.append((decision_id, bundle, context or {}))
            self.requested += 1

    def get(self, decision_id: str) -> Optional[dict]:
        with self._lock:
            r = self._results.get(decision_id)
            return dict(r) if r else None

    def is_actionable(self, decision: Optional[dict], *, now: Optional[float] = None) -> bool:
        """True only for a fresh, confident up/down decision (used by FOLLOW mode)."""
        if not decision or decision.get("action") not in ("up", "down"):
            return False
        if float(decision.get("confidence") or 0.0) < self.min_confidence:
            return False
        now = float(now if now is not None else time.time())
        age = now - float(decision.get("ts") or 0.0)
        return age <= float(decision.get("ttl_s") or self.ttl_s)

    # -- worker ------------------------------------------------------------- #
    def _process_one(self) -> bool:
        with self._lock:
            if not self._queue:
                return False
            decision_id, bundle, context = self._queue.popleft()
        if self._budget is not None and not self._budget.try_spend("decider"):
            with self._lock:
                self.skipped_budget += 1
            return True
        t0 = time.time()
        decision = None
        try:
            decision = self._fn(bundle)
        except Exception:  # noqa: BLE001 — fail-closed
            decision = None
        latency = time.time() - t0
        with self._lock:
            if decision is None:
                self.errors += 1
            else:
                decision["ts"] = time.time()
                decision["latency_s"] = round(latency, 3)
                decision["context"] = context or {}
                self.decided += 1
                self.latency_sum += latency
                self._results[decision_id] = decision
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

    def start(self) -> "GrokDecider":
        if self.mode != "off" and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="grok-decider", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # -- grading ------------------------------------------------------------ #
    def grade(self, decision_id: str, *, outcome_up: bool, pnl: Optional[float] = None) -> None:
        """Grade by decision_id using the in-memory result cache (used by unit tests)."""
        with self._lock:
            dec = self._results.get(decision_id)
        if not dec:
            return
        self.grade_fields(action=dec.get("action"), p_up=dec.get("p_up"),
                          context=dec.get("context") or {}, outcome_up=outcome_up, pnl=pnl)

    def grade_fields(self, *, action: Optional[str], p_up, context: Optional[dict],
                     outcome_up: bool, pnl: Optional[float] = None) -> None:
        """Grade from explicit snapshot fields (restart-safe — the engine persists these in its
        pending-grade list, so grades survive a process restart). Always grades the directional VIEW
        (p_up) vs the realized close; grades the ACTION only for up/down."""
        if action is None:
            return
        with self._lock:
            dec = {"action": action, "p_up": p_up, "context": context or {}}
            b = self.by_action.setdefault(action, {"n": 0, "wins": 0, "pnl": 0.0})
            b["n"] += 1
            if pnl is not None:
                b["pnl"] = round(b["pnl"] + float(pnl), 6)
            # (1) ALWAYS grade the directional VIEW (p_up) vs the realized outcome — this is the
            # rich edge data that accrues every window even when the action is no_trade.
            p_up = float(dec.get("p_up") if dec.get("p_up") is not None else 0.5)
            view_correct = (p_up > 0.5) == bool(outcome_up)
            self.view_graded += 1
            self.view_correct += int(view_correct)
            self.view_brier_sum += (p_up - (1.0 if outcome_up else 0.0)) ** 2
            for dim, bucket in (dec.get("context") or {}).items():
                if bucket is None:
                    continue
                cb = self.by_context.setdefault(dim, {}).setdefault(str(bucket), {"n": 0, "correct": 0})
                cb["n"] += 1
                cb["correct"] += int(view_correct)
            self._recent.append({"action": action, "p_up": round(p_up, 3),
                                 "confidence": round(float(dec.get("confidence") or 0.0), 3),
                                 "outcome_up": bool(outcome_up), "view_correct": bool(view_correct),
                                 "context": dec.get("context") or {}})
            # (2) ACTION-level grading (only for up/down trades the bot would actually take)
            if action == "no_trade":
                self.abstains += 1
                return
            ap = p_up if action == "up" else (1.0 - p_up)
            correct = (action == "up") == bool(outcome_up)
            self.graded += 1
            self.correct += int(correct)
            self.brier_sum += (ap - (1.0 if outcome_up else 0.0)) ** 2
            b["wins"] += int(correct)

    def _view_edge_candidates_locked(self) -> list:
        """Contexts where Grok's directional VIEW is a STATISTICALLY real edge: enough graded views
        and a Wilson lower bound of accuracy > 0.5 (profit-discovery trigger). Observe-only — flags
        where it would be reasonable to start trading the view; never auto-acts."""
        out = []
        for dim, buckets in self.by_context.items():
            for b, s in buckets.items():
                n = s["n"]
                if n < self.view_promote_min_samples:
                    continue
                lo = _wilson_lower(s["correct"], n, 1.64)
                if lo is not None and lo > 0.5:
                    out.append({"dimension": dim, "bucket": b, "n": n,
                                "accuracy": round(s["correct"] / n, 4),
                                "accuracy_lower_ci": round(lo, 4)})
        out.sort(key=lambda r: r["accuracy_lower_ci"], reverse=True)
        return out

    def context_policy(self, context: Optional[dict], *, min_samples: Optional[int] = None,
                       margin: Optional[float] = None) -> dict:
        """Self-improving closed loop: from the live per-context VIEW accuracy decide how to act in
        THIS context — ``exploit`` (proven edge: Wilson lower > 0.5+margin -> act on Grok's view,
        size up by edge strength), ``avoid`` (proven losing: Wilson upper < 0.5-margin -> skip), or
        ``explore`` (uncertain/cold -> sample). This concentrates trading where edge is proven and
        stops wasting trades where it isn't — active learning, not blind exploration."""
        ms = self.adaptive_min_samples if min_samples is None else int(min_samples)
        # exploit bar loosens as aggression rises (winning) and tightens on losses
        mg = self.aggr.exploit_margin(self.adaptive_margin) if margin is None else float(margin)
        best = None       # (dim, bucket, n, lower)
        worst_upper = None
        with self._lock:
            for dim, bucket in (context or {}).items():
                if bucket is None:
                    continue
                s = self.by_context.get(dim, {}).get(str(bucket))
                if not s or s["n"] < ms:
                    continue
                lo = _wilson_lower(s["correct"], s["n"], 1.64)
                up = _wilson_upper(s["correct"], s["n"], 1.64)
                if lo is not None and (best is None or lo > best[3]):
                    best = (dim, str(bucket), s["n"], lo)
                if up is not None and (worst_upper is None or up < worst_upper[2]):
                    worst_upper = (dim, str(bucket), up, s["n"])
        if best is not None and best[3] > 0.5 + mg:
            size_mult = min(2.0, 1.0 + (best[3] - 0.5) * 4.0)
            return {"mode": "exploit", "dimension": best[0], "bucket": best[1], "n": best[2],
                    "accuracy_lower_ci": round(best[3], 4), "size_mult": round(size_mult, 2)}
        if worst_upper is not None and worst_upper[2] < 0.5 - mg:
            return {"mode": "avoid", "dimension": worst_upper[0], "bucket": worst_upper[1],
                    "n": worst_upper[3], "accuracy_upper_ci": round(worst_upper[2], 4)}
        return {"mode": "explore"}

    def report(self) -> dict:
        with self._lock:
            acc = round(self.correct / self.graded, 4) if self.graded else None
            brier = round(self.brier_sum / self.graded, 4) if self.graded else None
            v_acc = round(self.view_correct / self.view_graded, 4) if self.view_graded else None
            v_brier = round(self.view_brier_sum / self.view_graded, 4) if self.view_graded else None
            avg_lat = round(self.latency_sum / self.decided, 3) if self.decided else None
            by_action = {a: {"n": s["n"],
                             "direction_accuracy": (round(s["wins"] / s["n"], 4)
                                                    if s["n"] and a != "no_trade" else None),
                             "pnl_usd": round(s["pnl"], 4)}
                         for a, s in self.by_action.items()}
            return {
                "enabled": self.mode != "off", "mode": self.mode, "paper_only": True,
                "affects_trading": False,
                "fail_closed": True, "min_confidence": self.min_confidence, "ttl_s": self.ttl_s,
                "requested": self.requested, "decided": self.decided, "errors": self.errors,
                "skipped_budget": self.skipped_budget, "pending": len(self._queue),
                "avg_latency_s": avg_lat,
                "graded_directional": self.graded, "direction_accuracy": acc, "brier": brier,
                "views_graded": self.view_graded, "view_accuracy": v_acc, "view_brier": v_brier,
                "view_edge_candidates": self._view_edge_candidates_locked(),
                "aggression": self.aggr.status(),
                "abstains": self.abstains, "by_action": by_action,
                "accuracy_by_context": {
                    dim: {b: {"n": s["n"],
                              "accuracy": (round(s["correct"] / s["n"], 4) if s["n"] else None)}
                          for b, s in buckets.items()}
                    for dim, buckets in self.by_context.items()},
                "recent_decisions": list(self._recent),
                "note": ("Grok decider is observe-only (shadow): decide+grade per window; never "
                         "places or sizes trades. Fail-closed -> no_trade in the advisory payload."),
            }

    def to_state(self) -> dict:
        with self._lock:
            return {"requested": self.requested, "decided": self.decided, "errors": self.errors,
                    "skipped_budget": self.skipped_budget, "latency_sum": round(self.latency_sum, 3),
                    "graded": self.graded, "correct": self.correct,
                    "brier_sum": round(self.brier_sum, 6), "abstains": self.abstains,
                    "view_graded": self.view_graded, "view_correct": self.view_correct,
                    "view_brier_sum": round(self.view_brier_sum, 6),
                    "aggression": self.aggr.to_state(),
                    "by_action": {a: dict(s) for a, s in self.by_action.items()},
                    "by_context": {d: {b: dict(s) for b, s in bk.items()}
                                   for d, bk in self.by_context.items()},
                    "recent": list(self._recent)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        with self._lock:
            self.requested = int(data.get("requested", 0) or 0)
            self.decided = int(data.get("decided", 0) or 0)
            self.errors = int(data.get("errors", 0) or 0)
            self.skipped_budget = int(data.get("skipped_budget", 0) or 0)
            self.latency_sum = float(data.get("latency_sum", 0.0) or 0.0)
            self.graded = int(data.get("graded", 0) or 0)
            self.correct = int(data.get("correct", 0) or 0)
            self.brier_sum = float(data.get("brier_sum", 0.0) or 0.0)
            self.abstains = int(data.get("abstains", 0) or 0)
            self.view_graded = int(data.get("view_graded", 0) or 0)
            self.view_correct = int(data.get("view_correct", 0) or 0)
            self.view_brier_sum = float(data.get("view_brier_sum", 0.0) or 0.0)
            self.aggr.load_state(data.get("aggression") or {})
            self.by_action = {a: {"n": int(s.get("n", 0) or 0), "wins": int(s.get("wins", 0) or 0),
                                  "pnl": float(s.get("pnl", 0.0) or 0.0)}
                              for a, s in (data.get("by_action") or {}).items()}
            self.by_context = {d: {b: {"n": int(s.get("n", 0) or 0),
                                       "correct": int(s.get("correct", 0) or 0)}
                                   for b, s in bk.items()}
                               for d, bk in (data.get("by_context") or {}).items()}
            self._recent = deque((data.get("recent") or []), maxlen=12)
