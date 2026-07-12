"""Monte Carlo pricing for directional BTC/ETH pulse (PAPER ONLY; deterministic CODE).

Closed-form digital_p_up (Gaussian GBM) is exact for pure diffusion — plain MC of the
same model just reproduces it. MC earns its keep when Grok (or leads) supply drift / jumps /
vol multipliers that break the Gaussian assumption.

Roles:
  * CODE runs the simulator (seedable, reproducible)
  * Grok PARAMETERIZES bounded scenario params (mu, sigma_mult, jumps) — never places trades
  * Bot grades p_mc vs settled Chainlink outcomes and self-tunes blend weights

Directional only — dep-arb / dutch-book MC paths removed.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

try:
    import numpy as np
    HAVE_NUMPY = True
except Exception:  # noqa: BLE001 — never break import if numpy is absent
    np = None  # type: ignore
    HAVE_NUMPY = False


def _require_numpy() -> None:
    if not HAVE_NUMPY:
        raise RuntimeError("monte_carlo requires numpy; MC unavailable")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def closed_form_digital_p_up(s_now: float, s_open: float, sigma_per_sec: float,
                             r_seconds: float, *, mu_per_sec: float = 0.0) -> float:
    """Analytic P(close >= open) for GBM — same model as fair_value.digital_p_up."""
    if r_seconds <= 0 or sigma_per_sec <= 0 or s_now <= 0 or s_open <= 0:
        return 1.0 if s_now >= s_open else 0.0
    sig_h = sigma_per_sec * math.sqrt(r_seconds)
    z = (math.log(s_now / s_open) + (mu_per_sec - 0.5 * sigma_per_sec ** 2) * r_seconds) / sig_h
    return max(0.0, min(1.0, _norm_cdf(z)))


def _rng(seed: Optional[int]):
    return np.random.default_rng(seed)


def _terminal_log_returns(sigma_per_sec: float, seconds: float, n_paths: int, *,
                          mu_per_sec: float = 0.0, jump_intensity_per_sec: float = 0.0,
                          jump_sigma: float = 0.0, rng=None):
    """Terminal log-return over seconds for n_paths GBM draws + optional jumps."""
    rng = rng if rng is not None else _rng(None)
    drift = (mu_per_sec - 0.5 * sigma_per_sec ** 2) * seconds
    diff = rng.normal(drift, sigma_per_sec * math.sqrt(seconds), size=n_paths)
    if jump_intensity_per_sec > 0.0 and jump_sigma > 0.0:
        n_jumps = rng.poisson(jump_intensity_per_sec * seconds, size=n_paths)
        diff = diff + rng.normal(0.0, 1.0, size=n_paths) * (np.sqrt(n_jumps) * jump_sigma)
    return diff


def mc_digital_p_up(s_now: float, s_open: float, sigma_per_sec: float, r_seconds: float, *,
                    mu_per_sec: float = 0.0, n_paths: int = 20000, seed: Optional[int] = None,
                    jump_intensity_per_sec: float = 0.0, jump_sigma: float = 0.0) -> float:
    """MC estimate of P(close >= open). Converges to closed form without jumps."""
    _require_numpy()
    if r_seconds <= 0 or sigma_per_sec <= 0:
        return 1.0 if s_now >= s_open else 0.0
    r = _terminal_log_returns(sigma_per_sec, r_seconds, int(n_paths), mu_per_sec=mu_per_sec,
                              jump_intensity_per_sec=jump_intensity_per_sec,
                              jump_sigma=jump_sigma, rng=_rng(seed))
    s_close = s_now * np.exp(r)
    return float(np.mean(s_close >= s_open))


def mc_directional_p_up(
    s_now: float,
    s_open: float,
    sigma_per_sec: float,
    r_seconds: float,
    *,
    mu_per_sec: float = 0.0,
    sigma_mult: float = 1.0,
    jump_intensity_per_sec: float = 0.0,
    jump_sigma: float = 0.0,
    n_paths: int = 8000,
    seed: Optional[int] = None,
    crash_threshold_pct: float = 1.5,
    control_alpha: float = 0.5,
    n_steps: int = 8,
) -> dict:
    """Directional path MC: P(close >= open) + crash prob + control-variate blend."""
    _require_numpy()
    out = {
        "available": False, "p_mc": None, "p_digital": None, "p_mc_adj": None,
        "p_crash": None, "se": None, "n_paths": int(n_paths), "sigma_eff": None,
    }
    if s_now is None or s_open is None or s_now <= 0 or s_open <= 0:
        out["reason"] = "bad_price"
        return out
    if r_seconds <= 0:
        p = 1.0 if s_now >= s_open else 0.0
        out.update({"available": True, "p_mc": p, "p_digital": p, "p_mc_adj": p,
                    "p_crash": 0.0, "se": 0.0, "reason": "window_closed"})
        return out
    sig = float(sigma_per_sec) * max(0.5, min(2.0, float(sigma_mult or 1.0)))
    if sig <= 0:
        out["reason"] = "no_vol"
        return out
    n = max(500, int(n_paths))
    steps = max(1, int(n_steps))
    dt = float(r_seconds) / steps
    rng = _rng(seed)
    log_s = np.full(n, math.log(float(s_now)))
    trough = np.full(n, float(s_now))
    mu = float(mu_per_sec or 0.0)
    ji = max(0.0, float(jump_intensity_per_sec or 0.0))
    js = max(0.0, float(jump_sigma or 0.0))
    for _ in range(steps):
        drift = (mu - 0.5 * sig * sig) * dt
        step = rng.normal(drift, sig * math.sqrt(dt), size=n)
        if ji > 0.0 and js > 0.0:
            nj = rng.poisson(ji * dt, size=n)
            step = step + rng.normal(0.0, 1.0, size=n) * (np.sqrt(nj) * js)
        log_s = log_s + step
        px = np.exp(log_s)
        trough = np.minimum(trough, px)
    s_close = np.exp(log_s)
    up = s_close >= float(s_open)
    p_mc = float(np.mean(up))
    se = float(math.sqrt(max(0.0, p_mc * (1.0 - p_mc) / n)))
    dd = (float(s_now) - trough) / float(s_now)
    thr = max(0.001, float(crash_threshold_pct) / 100.0)
    p_crash = float(np.mean(dd >= thr))
    p_dig = closed_form_digital_p_up(
        float(s_now), float(s_open), sig, float(r_seconds), mu_per_sec=mu)
    a = max(0.0, min(1.0, float(control_alpha)))
    if ji > 0.0 and js > 0.0:
        p_adj = p_mc
    else:
        p_adj = float(a * p_dig + (1.0 - a) * p_mc)
    out.update({
        "available": True,
        "p_mc": round(p_mc, 6),
        "p_digital": round(p_dig, 6),
        "p_mc_adj": round(max(0.0, min(1.0, p_adj)), 6),
        "p_crash": round(p_crash, 6),
        "se": round(se, 6),
        "sigma_eff": sig,
        "crash_threshold_pct": float(crash_threshold_pct),
        "mu_per_sec": mu,
        "sigma_mult": float(sigma_mult or 1.0),
        "jump_intensity_per_sec": ji,
        "jump_sigma": js,
    })
    return out


def simulate_prices_at_times(s_now: float, now: float, times: Sequence[float],
                             sigma_per_sec: float, *, mu_per_sec: float = 0.0,
                             n_paths: int = 20000, rng=None,
                             jump_intensity_per_sec: float = 0.0, jump_sigma: float = 0.0):
    """Simulate correlated GBM prices at future timestamps (shared Brownian path per draw)."""
    _require_numpy()
    rng = rng if rng is not None else _rng(None)
    ts_sorted = sorted({float(t) for t in times if float(t) > now})
    if not ts_sorted:
        return np.empty((int(n_paths), 0)), {}
    prev = float(now)
    cum = np.zeros(int(n_paths))
    cols = []
    for t in ts_sorted:
        dt = max(1e-9, t - prev)
        drift = (mu_per_sec - 0.5 * sigma_per_sec ** 2) * dt
        step = rng.normal(drift, sigma_per_sec * math.sqrt(dt), size=int(n_paths))
        if jump_intensity_per_sec > 0.0 and jump_sigma > 0.0:
            nj = rng.poisson(jump_intensity_per_sec * dt, size=int(n_paths))
            step = step + rng.normal(0.0, 1.0, size=int(n_paths)) * (np.sqrt(nj) * jump_sigma)
        cum = cum + step
        cols.append(s_now * np.exp(cum))
        prev = t
    prices = np.stack(cols, axis=1)
    return prices, {t: i for i, t in enumerate(ts_sorted)}



# ---- LLM-parameterized scenario (LLM = modeler; deterministic code = simulator) ---------------- #
NEUTRAL_SCENARIO = {"sigma_mult": 1.0, "mu_per_sec": 0.0, "jump_intensity_per_sec": 0.0,
                    "jump_sigma": 0.0, "lean": "none", "crash_threshold_pct": 1.5,
                    "confidence": 0.0, "source": "neutral"}

# Tight bounds so an LLM (esp. an anti-predictive one) can shade the model, never hijack it.
_SCENARIO_BOUNDS = {
    "sigma_mult": (0.5, 2.0), "mu_per_sec": (-5e-6, 5e-6),
    "jump_intensity_per_sec": (0.0, 0.05), "jump_sigma": (0.0, 0.01),
}


def validate_scenario_params(d, *, source: str = "llm") -> dict:
    """Clamp an LLM's proposed MC parameters into safe bounds; fall back to neutral on bad input.
    Keeps the LLM as a bounded *modeler* — it can tilt vol/drift/tail risk but not blow up the sim."""
    if not isinstance(d, dict):
        return dict(NEUTRAL_SCENARIO)
    out = {}
    for k, (lo, hi) in _SCENARIO_BOUNDS.items():
        try:
            v = float(d.get(k, NEUTRAL_SCENARIO[k]))
        except (TypeError, ValueError):
            v = NEUTRAL_SCENARIO[k]
        out[k] = max(lo, min(hi, v))
    lean = str(d.get("lean") or "none").strip().lower()
    out["lean"] = lean if lean in ("up", "down", "none") else "none"
    try:
        crash = float(d.get("crash_threshold_pct", 1.5))
    except (TypeError, ValueError):
        crash = 1.5
    out["crash_threshold_pct"] = max(0.5, min(5.0, crash))
    try:
        conf = float(d.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = max(0.0, min(1.0, conf))
    out["source"] = str(d.get("source") or source)[:24]
    return out


def _scenario_prompt(context: dict) -> str:
    return (
        "You parameterize a Monte Carlo model for a Polymarket BTC/ETH up/down window "
        "(settles on Chainlink close vs open). Base model is Gaussian GBM. "
        "Use ALL context. Read tv_alert_interpretation (composite_lean + signal_agreement + "
        "Cardwell hints) first. PLOT tv_5m_price_pattern.short_path "
        "(oldest→newest OHLC) as the current price pattern; use regime_path_tail as HTF; "
        "tv_rsi_band: analyze RSI 30/70 overbought/oversold (oversold→up lean, "
        "overbought→down lean, band_event crosses); "
        "tv_rsi_divergence: read primer.tradingview_official (Wilder/Cardwell) + "
        "operator_indicator Pine logic; regular bull/bear pivot disagreement; "
        "confirm_fade_by_side; "
        "tv_rsi_overlay is confirm/fade only (not the trend). "
        "Also Chainlink, CEX leads, Poly mid, digital p. "
        "Return STRICT JSON ONLY: "
        "{\"sigma_mult\":<0.5-2.0>,\"mu_per_sec\":<-5e-6..5e-6>,"
        "\"jump_intensity_per_sec\":<0-0.05>,\"jump_sigma\":<0-0.01>,"
        "\"lean\":\"up|down|none\",\"crash_threshold_pct\":<0.5-5.0>,"
        "\"confidence\":<0-1>}. "
        "Neutral = {sigma_mult:1,mu_per_sec:0,jump_intensity_per_sec:0,jump_sigma:0,"
        "lean:none,crash_threshold_pct:1.5,confidence:0}. "
        "Only deviate on clear evidence. Do NOT output a trade.\nCONTEXT: "
        + str(context)[:4000])


def make_grok_scenario_fn(*, model: str = "grok-4.3", timeout_s: float = 15.0, chat=None):
    """Build ``fn(context) -> validated scenario params | None``. Asks the LLM to shade the MC's BTC
    return model for the next ~15 min from recent regime/news. Fail-open (None on any error)."""
    from engine.pulse.grok_intel import _grok_chat, _parse_json
    chat = chat if chat is not None else _grok_chat
    box: dict = {}

    def _fn(context: dict) -> Optional[dict]:
        d = _parse_json(chat(_scenario_prompt(context), model=model, timeout_s=timeout_s, box=box))
        if not isinstance(d, dict):
            return None
        return validate_scenario_params(d, source="grok")
    return _fn


def make_claude_scenario_fn(*, model: Optional[str] = None, timeout_s: float = 15.0, chat=None):
    """Claude version of the MC scenario advisor -- a SECOND, independent LLM opinion on the MC params
    (Grok has graded anti-predictive on direction, so a different model is worth ensembling). Fail-open."""
    from engine.pulse.grok_intel import _parse_json
    from engine.pulse.claude_client import claude_chat as _cc
    chat = chat if chat is not None else _cc
    box: dict = {}

    def _fn(context: dict) -> Optional[dict]:
        try:
            txt = chat(_scenario_prompt(context), model=model, timeout_s=timeout_s, box=box)
        except Exception:  # noqa: BLE001
            return None
        d = _parse_json(txt)
        if not isinstance(d, dict):
            return None
        return validate_scenario_params(d, source="claude")
    return _fn


def make_ensemble_scenario_fn(fns):
    """Average the validated params from multiple LLM scenario fns (dual-LLM ensemble). Uses whichever
    respond; falls back to None if none do. Averaging two independent modelers is more robust than one
    (esp. when one LLM is noisy) -- the forecast-combination principle applied to MC params."""
    fns = [f for f in (fns or []) if f is not None]

    def _fn(context: dict) -> Optional[dict]:
        params = []
        srcs = []
        for f in fns:
            try:
                p = f(context)
            except Exception:  # noqa: BLE001
                p = None
            if isinstance(p, dict):
                params.append(validate_scenario_params(p, source=p.get("source", "llm")))
                srcs.append(str(p.get("source", "llm")))
        if not params:
            return None
        keys = ("sigma_mult", "mu_per_sec", "jump_intensity_per_sec", "jump_sigma")
        avg = {k: sum(float(p.get(k, 0.0) or 0.0) for p in params) / len(params) for k in keys}
        avg["source"] = "+".join(sorted(set(srcs)))
        return avg
    return _fn


class MCScenarioAdvisor:
    """Periodic LLM proposal of bounded MC scenario params, cached + fail-open to neutral. Runs on a
    background worker; the tick reads ``latest()`` (never blocks). PAPER; observe/advisory on params
    -- the MC still runs deterministically in code."""

    def __init__(self, *, scenario_fn=None, budget=None, context_fn=None,
                 interval_s: float = 300.0, max_age_s: float = 900.0, feature: str = "mc_scenario"):
        import threading
        import time as _t
        self._t = _t
        self._fn = scenario_fn if scenario_fn is not None else make_grok_scenario_fn()
        self._budget = budget
        self._context_fn = context_fn
        self.interval_s = max(60.0, float(interval_s))
        self.max_age_s = float(max_age_s)
        self.feature = feature
        self._lock = threading.Lock()
        self._params = dict(NEUTRAL_SCENARIO)
        self._ts = 0.0
        self.calls = 0
        self.errors = 0
        self.skipped_budget = 0
        self._stop = threading.Event()
        self._thread = None
        self._threading = threading

    def refresh(self) -> Optional[dict]:
        if self._budget is not None and not self._budget.try_spend(self.feature):
            self.skipped_budget += 1
            return None
        ctx = {}
        try:
            ctx = self._context_fn() if self._context_fn else {}
        except Exception:  # noqa: BLE001
            ctx = {}
        p = None
        try:
            p = self._fn(ctx)
        except Exception:  # noqa: BLE001
            p = None
        if p is None:
            self.errors += 1
        else:
            self.calls += 1
            with self._lock:
                self._params, self._ts = validate_scenario_params(p, source=p.get("source", "llm")), self._t.time()
        return p

    def latest(self) -> dict:
        with self._lock:
            if self._ts and (self._t.time() - self._ts) <= self.max_age_s:
                return dict(self._params)
        return dict(NEUTRAL_SCENARIO)

    def _worker(self) -> None:
        self._stop.wait(min(self.interval_s, 20.0))
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.interval_s)

    def start(self) -> "MCScenarioAdvisor":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = self._threading.Thread(target=self._worker, name="mc-scenario", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def report(self) -> dict:
        with self._lock:
            return {"enabled": True, "calls": self.calls, "errors": self.errors,
                    "skipped_budget": self.skipped_budget, "params": dict(self._params),
                    "age_s": (round(self._t.time() - self._ts, 1) if self._ts else None)}


def pnl_summary(prob_win: float, entry_price: float, *, size_usd: float = 1.0,
                n_paths: int = 20000, seed: Optional[int] = None) -> dict:
    """Full P&L distribution + Kelly for a single binary payoff bought at ``entry_price``.
    win -> +(1-entry)*shares ; lose -> -entry*shares, shares = size_usd/entry."""
    _require_numpy()
    p = max(0.0, min(1.0, float(prob_win)))
    entry = min(0.999, max(1e-6, float(entry_price)))
    shares = float(size_usd) / entry
    wins = _rng(seed).random(int(n_paths)) < p
    pnl = np.where(wins, (1.0 - entry) * shares, -entry * shares)
    b = (1.0 - entry) / entry                       # net odds
    kelly = (p * (b + 1.0) - 1.0) / b if b > 0 else 0.0
    return {
        "prob_win": round(p, 4), "entry_price": round(entry, 4),
        "expected_pnl_usd": round(float(pnl.mean()), 4),
        "std_pnl_usd": round(float(pnl.std()), 4),
        "q05_pnl_usd": round(float(np.quantile(pnl, 0.05)), 4),
        "median_pnl_usd": round(float(np.quantile(pnl, 0.50)), 4),
        "q95_pnl_usd": round(float(np.quantile(pnl, 0.95)), 4),
        "prob_loss": round(float((pnl < 0).mean()), 4),
        "kelly_fraction": round(max(0.0, min(1.0, kelly)), 4),
    }
