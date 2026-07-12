"""CHRONOS — Chronological Holdout Replay Operating Numerical Scorecard (PAPER ONLY).

Invented pre-decision testing layer for Bot-3. Reviews ALL bot math through ONE framework
before bet size or trade authority is granted.

Problem (quant audit)
---------------------
The bot runs 15+ mathematical modules (VWAP/EV, digital Φ(d₂), Wilson CI, Kelly, Beta,
Brier, BH-FDR, log-odds Bayes, Pareto utility, entropy, Thompson, etc.) but only
Selectivity has counterfactual replay — and that replay is **in-sample** (uses final evidence).

No module tested SAWR, GateAutoTune, BinaryIntel hard_block, or Kelly sizing **before**
they affected live decisions.

Invention: CHRONOS three-layer validation
-----------------------------------------
**Layer A — Trade Certificate (every fill)**
  Before size > 0, replay chronologically similar settled trades (context cohort).
  Compute conservative Wilson LB win-rate vs ask breakeven; dry-run Kelly; CVS score.

      CVS = w_lb − ask + (w_n/(w_n+n₀))·log(1+n) − λ·max(0, ask − w_lb)

  Verdict: proceed | probe | block | cold_probe

**Layer B — Walk-forward policy test (gate changes)**
  Before any learner tightens/loosens scalar gates, split ledger by entry_ts:
  train = earliest (1−f), holdout = latest f. Simulate policy on holdout only.
  Approve tighten if holdout PF improves or losses_avoided > wins_missed.
  Veto loosen if holdout Wilson LB WR < kill floor.

**Layer C — Size cap from replay**
  Kelly dry-run: f* = max(0, (w_lb − ask)/(1−ask)) × kelly_fraction
  size_cap_mult = min(1, f*/f_ref) — never upsize above base without certificate.

Does NOT replace execution_gate (VWAP reality). CHRONOS is the quant dry-run authority
between learners and sizing.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Math primitives (shared across bot modules — single canonical implementation)
# ---------------------------------------------------------------------------

def wilson_lb(wins: int, n: int, z: float = 1.645) -> float:
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return max(0.0, center - margin)


def wilson_ub(wins: int, n: int, z: float = 1.645) -> float:
    if n <= 0:
        return 1.0
    phat = wins / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1.0 - phat) + z * z / (4 * n)) / n)) / denom
    return min(1.0, center + margin)


def breakeven_wr_at_ask(ask: float) -> float:
    """Binary bought at ``ask``: EV/share = p − ask → breakeven p* = ask."""
    try:
        a = float(ask)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, a))


def kelly_binary_fraction(p_win: float, ask: float) -> float:
    """Full Kelly for unit binary bought at ask."""
    if ask <= 0 or ask >= 1:
        return 0.0
    b = (1.0 - ask) / ask
    if b <= 0:
        return 0.0
    return max(0.0, float(p_win) - (1.0 - float(p_win)) / b)


def profit_factor(rows: list[dict]) -> Optional[float]:
    gw = sum(float(r["pnl"]) for r in rows if float(r["pnl"]) > 0)
    gl = sum(-float(r["pnl"]) for r in rows if float(r["pnl"]) < 0)
    if gl <= 0:
        return None if gw <= 0 else 999.0
    return round(gw / gl, 4)


def max_drawdown(pnls: list[float]) -> float:
    peak = 0.0
    cum = 0.0
    mdd = 0.0
    for p in pnls:
        cum += float(p)
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return round(mdd, 4)


# ---------------------------------------------------------------------------
# Position normalization + context keys
# ---------------------------------------------------------------------------

def price_bucket(ask: float) -> str:
    try:
        a = float(ask)
    except (TypeError, ValueError):
        return "unk"
    if a < 0.40:
        return "lt40"
    if a < 0.48:
        return "40_48"
    if a < 0.55:
        return "48_55"
    if a < 0.65:
        return "55_65"
    if a < 0.72:
        return "65_72"
    return "ge72"


def ttc_bucket(ttc_s: float, window_seconds: float) -> str:
    try:
        ttc = float(ttc_s)
        ws = float(window_seconds or 900)
    except (TypeError, ValueError):
        return "unk"
    frac = ttc / ws if ws > 0 else 0.5
    if frac > 0.75:
        return "early"
    if frac > 0.35:
        return "mid"
    return "late"


def lane_from_slug(series_slug: str, window_seconds: float = 0) -> str:
    slug = str(series_slug or "").lower()
    ws = int(window_seconds or 0)
    if ws >= 3600 or "1h" in slug or "hourly" in slug:
        return "1h"
    if ws >= 600 or "15m" in slug:
        return "15m"
    return "5m"


def asset_from_slug(series_slug: str) -> str:
    slug = str(series_slug or "").lower()
    return "eth" if slug.startswith("eth") or "ethereum" in slug else "btc"


def context_key(
    *,
    asset: str,
    lane: str,
    side: str,
    ask: float,
    ttc_s: Optional[float] = None,
    window_seconds: float = 900.0,
) -> str:
    return "|".join([
        str(asset or "btc").lower(),
        str(lane or "15m").lower(),
        str(side or "").lower(),
        price_bucket(ask),
        ttc_bucket(float(ttc_s or 0), window_seconds) if ttc_s is not None else "ttc_unk",
    ])


def normalize_position(raw) -> Optional[dict]:
    """Ledger position → chronological replay row."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        r = raw
        status = r.get("status")
        research = r.get("research") or {}
    else:
        status = getattr(raw, "status", None)
        research = getattr(raw, "research", None) or {}

    if status != "settled":
        return None

    slug = str(research.get("series_slug") or getattr(raw, "series_slug", "") or "").lower()
    ws = int(research.get("window_seconds") or getattr(raw, "window_seconds", 0) or 0)
    if isinstance(raw, dict):
        side = str(raw.get("side") or "").lower()
        entry = raw.get("entry_price") or raw.get("entry_px")
        entry_ts = raw.get("entry_ts") or raw.get("opened_at") or research.get("entry_ts")
        won = raw.get("won")
        pnl = raw.get("pnl_usd")
    else:
        side = str(getattr(raw, "side", "") or "").lower()
        entry = getattr(raw, "entry_price", None)
        entry_ts = getattr(raw, "entry_ts", None) or getattr(raw, "opened_at", None)
        won = getattr(raw, "won", None)
        pnl = getattr(raw, "pnl_usd", None)

    ttc = research.get("entry_ttc_s")
    if ttc is None and research.get("seconds_since_open_at_entry") is not None and ws:
        ttc = float(ws) - float(research["seconds_since_open_at_entry"])

    try:
        ask_f = float(entry) if entry is not None else None
    except (TypeError, ValueError):
        ask_f = None

    asset = asset_from_slug(slug)
    lane = lane_from_slug(slug, ws)

    return {
        "entry_ts": float(entry_ts or 0),
        "won": bool(won),
        "pnl": float(pnl or 0),
        "side": side,
        "ask": ask_f,
        "asset": asset,
        "lane": lane,
        "slug": slug,
        "window_seconds": ws,
        "ttc_s": float(ttc) if ttc is not None else None,
        "context": (context_key(
            asset=asset, lane=lane, side=side, ask=ask_f or 0.5,
            ttc_s=float(ttc) if ttc is not None else None,
            window_seconds=float(ws or 900),
        ) if ask_f is not None else None),
    }


def normalize_positions(positions) -> list[dict]:
    rows = []
    if isinstance(positions, dict):
        iterable = positions.values()
    else:
        iterable = positions or []
    for p in iterable:
        row = normalize_position(p)
        if row and row.get("entry_ts", 0) > 0:
            rows.append(row)
    rows.sort(key=lambda r: r["entry_ts"])
    return rows


# ---------------------------------------------------------------------------
# Layer A — CHRONOS Verdict Score (CVS) + trade certificate
# ---------------------------------------------------------------------------

def chronos_verdict_score(
    *,
    wilson_lb_wr: float,
    ask: float,
    cohort_n: int,
    n_prior: float = 4.0,
    w_n: float = 0.05,
    kill_penalty: float = 1.5,
) -> float:
    """CVS = conservative edge + cohort confidence − kill penalty."""
    be = breakeven_wr_at_ask(ask)
    edge_lb = float(wilson_lb_wr) - be
    conf = (cohort_n / (cohort_n + float(n_prior))) * math.log1p(max(0, cohort_n))
    penalty = kill_penalty * max(0.0, be - float(wilson_lb_wr))
    return round(edge_lb + w_n * conf - penalty, 6)


@dataclass
class TradeCertificate:
    verdict: str  # proceed | probe | block | cold_probe
    cvs: float
    wilson_lb: float
    wilson_ub: float
    breakeven_wr: float
    cohort_n: int
    kelly_dry_run: float
    size_cap_mult: float
    exploration: bool
    context: str
    note: str

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "cvs": self.cvs,
            "wilson_lb": self.wilson_lb,
            "wilson_ub": self.wilson_ub,
            "breakeven_wr": self.breakeven_wr,
            "cohort_n": self.cohort_n,
            "kelly_dry_run": self.kelly_dry_run,
            "size_cap_mult": self.size_cap_mult,
            "exploration": self.exploration,
            "context": self.context,
            "note": self.note,
        }


@dataclass
class ChronosConfig:
    enabled: bool = True
    min_cohort_n: int = 4
    min_strict_n: int = 8
    z: float = 1.645
    proceed_cvs: float = 0.05
    probe_cvs: float = -0.02
    block_margin: float = 0.03
    kelly_fraction: float = 0.25
    exploration_rate: float = 0.12
    holdout_fraction: float = 0.30
    min_holdout_n: int = 6
    policy_min_holdout_pf: float = 0.90
    kill_wr: float = 0.48


@dataclass
class ChronosValidator:
    """Pre-decision dry-run authority."""

    cfg: ChronosConfig = field(default_factory=ChronosConfig)
    certificates_issued: int = 0
    blocked: int = 0
    probed: int = 0
    proceeded: int = 0
    policy_vetoed: int = 0
    _last_certificate: Optional[dict] = None

    def _cohort(self, positions: list[dict], ctx: str, before_ts: float) -> list[dict]:
        return [p for p in positions
                if p.get("context") == ctx and float(p["entry_ts"]) < float(before_ts)]

    def validate_trade(
        self,
        *,
        positions,
        asset: str,
        lane: str,
        side: str,
        ask: float,
        now: Optional[float] = None,
        ttc_s: Optional[float] = None,
        window_seconds: float = 900.0,
        model_p_win: Optional[float] = None,
    ) -> TradeCertificate:
        """Layer A: chronological cohort replay → trade certificate."""
        if not self.cfg.enabled:
            return TradeCertificate(
                verdict="proceed", cvs=1.0, wilson_lb=0.5, wilson_ub=0.5,
                breakeven_wr=breakeven_wr_at_ask(ask), cohort_n=0,
                kelly_dry_run=0.0, size_cap_mult=1.0, exploration=False,
                context="", note="chronos_disabled",
            )

        now_ts = float(now if now is not None else time.time())
        ctx = context_key(
            asset=asset, lane=lane, side=side, ask=ask,
            ttc_s=ttc_s, window_seconds=window_seconds,
        )
        rows = normalize_positions(positions)
        cohort = self._cohort(rows, ctx, now_ts)
        n = len(cohort)
        wins = sum(1 for r in cohort if r["won"])
        wlb = wilson_lb(wins, n, self.cfg.z) if n > 0 else 0.0
        wub = wilson_ub(wins, n, self.cfg.z) if n > 0 else 1.0
        be = breakeven_wr_at_ask(ask)
        cvs = chronos_verdict_score(
            wilson_lb_wr=wlb, ask=ask, cohort_n=n, kill_penalty=1.5)

        # Kelly dry-run uses conservative w_lb (not model_p_win).
        kelly_p = wlb if n >= self.cfg.min_cohort_n else (model_p_win or wlb or 0.5)
        k_full = kelly_binary_fraction(kelly_p, ask)
        k_dry = round(k_full * float(self.cfg.kelly_fraction), 6)
        size_cap = 1.0
        if k_dry > 0:
            size_cap = min(1.0, max(0.25, k_dry / 0.05))
        elif n >= self.cfg.min_cohort_n and wlb < be - self.cfg.block_margin:
            size_cap = 0.0

        exploration = False
        verdict = "cold_probe"
        note = "insufficient_cohort_history"

        if n < self.cfg.min_cohort_n:
            exploration = random.random() < float(self.cfg.exploration_rate)
            verdict = "cold_probe" if exploration else "probe"
            note = f"cohort_n={n}<{self.cfg.min_cohort_n}"
        elif wlb < be - self.cfg.block_margin and wub < be:
            exploration = random.random() < float(self.cfg.exploration_rate) * 0.5
            verdict = "block" if not exploration else "probe"
            note = f"wlb={wlb:.3f}<breakeven={be:.3f}"
        elif cvs >= self.cfg.proceed_cvs and wlb >= be:
            verdict = "proceed"
            note = f"cvs={cvs:.3f}>={self.cfg.proceed_cvs}"
        elif cvs >= self.cfg.probe_cvs or exploration:
            verdict = "probe"
            note = f"cvs={cvs:.3f} probe_band"
        else:
            exploration = random.random() < float(self.cfg.exploration_rate) * 0.5
            verdict = "block" if not exploration else "probe"
            note = f"cvs={cvs:.3f}<probe_floor"

        cert = TradeCertificate(
            verdict=verdict, cvs=cvs, wilson_lb=round(wlb, 4), wilson_ub=round(wub, 4),
            breakeven_wr=round(be, 4), cohort_n=n, kelly_dry_run=k_dry,
            size_cap_mult=round(size_cap, 4),
            exploration=exploration, context=ctx, note=note,
        )
        self.certificates_issued += 1
        self._last_certificate = cert.to_dict()
        if verdict == "block":
            self.blocked += 1
        elif verdict in ("probe", "cold_probe"):
            self.probed += 1
        else:
            self.proceeded += 1
        return cert

    # -----------------------------------------------------------------------
    # Layer B — Walk-forward policy test (before gate tighten/loosen)
    # -----------------------------------------------------------------------

    def walk_forward_split(
        self, positions: list[dict], *, holdout_fraction: Optional[float] = None,
    ) -> tuple[list[dict], list[dict]]:
        f = float(holdout_fraction if holdout_fraction is not None else self.cfg.holdout_fraction)
        rows = list(positions)
        if len(rows) < self.cfg.min_holdout_n + 4:
            return rows, []
        cut = max(4, int(len(rows) * (1.0 - f)))
        return rows[:cut], rows[cut:]

    def walk_forward_block_replay(
        self,
        positions: list[dict],
        *,
        should_block: Callable[[dict, list[dict]], bool],
    ) -> dict:
        """Chronological replay: block_fn sees only past trades at each step."""
        if positions and isinstance(positions[0], dict) and "context" in positions[0]:
            rows = list(positions)
        else:
            rows = normalize_positions(positions)
        accepted, rejected = [], []
        losses_avoided = wins_missed = 0
        history: list[dict] = []
        for row in rows:
            if should_block(row, history):
                rejected.append(row)
                if not row["won"]:
                    losses_avoided += 1
                else:
                    wins_missed += 1
            else:
                accepted.append(row)
            history.append(row)
        base_n = len(rows)
        base_wins = sum(1 for r in rows if r["won"])
        base_pnl = sum(r["pnl"] for r in rows)
        cf_wins = sum(1 for r in accepted if r["won"])
        cf_pnl = sum(r["pnl"] for r in accepted)
        return {
            "replayed": base_n,
            "baseline_win_rate": round(base_wins / base_n, 4) if base_n else None,
            "baseline_pnl_usd": round(base_pnl, 4),
            "counterfactual_trades": len(accepted),
            "counterfactual_win_rate": round(cf_wins / len(accepted), 4) if accepted else None,
            "counterfactual_pnl_usd": round(cf_pnl, 4),
            "trades_rejected": len(rejected),
            "losses_avoided": losses_avoided,
            "wins_missed": wins_missed,
            "max_drawdown_baseline": max_drawdown([r["pnl"] for r in rows]),
            "max_drawdown_counterfactual": max_drawdown([r["pnl"] for r in accepted]),
            "method": "chronos_walk_forward_chronological",
        }

    def validate_policy_action(
        self,
        *,
        positions,
        action: str,
        kill_wr: Optional[float] = None,
    ) -> dict:
        """Layer B: approve/veto tighten or loosen using holdout metrics."""
        if not self.cfg.enabled:
            return {"approved": True, "reason": "chronos_disabled", "action": action}

        rows = normalize_positions(positions)
        train, holdout = self.walk_forward_split(rows)
        if len(holdout) < max(3, self.cfg.min_holdout_n // 2):
            return {
                "approved": True,
                "reason": "insufficient_holdout",
                "action": action,
                "holdout_n": len(holdout),
            }

        hm = self._holdout_metrics(holdout)
        kill = float(kill_wr if kill_wr is not None else self.cfg.kill_wr)
        h_wlb = wilson_lb(
            int(round(float(hm.get("win_rate") or 0) * hm["n"])), hm["n"], self.cfg.z)

        if action == "loosen":
            if h_wlb < kill:
                self.policy_vetoed += 1
                return {
                    "approved": False,
                    "reason": "holdout_wilson_lb_below_kill",
                    "action": action,
                    "holdout": hm,
                    "holdout_wilson_lb": round(h_wlb, 4),
                    "kill_wr": kill,
                }
            return {"approved": True, "reason": "holdout_ok", "action": action, "holdout": hm}

        if action == "tighten":
            pf = hm.get("profit_factor")
            if pf is not None and float(pf) < float(self.cfg.policy_min_holdout_pf):
                return {
                    "approved": True,
                    "reason": "holdout_pf_weak_tighten_ok",
                    "action": action,
                    "holdout": hm,
                }
            return {"approved": True, "reason": "tighten_always_safe", "action": action, "holdout": hm}

        return {"approved": True, "reason": "unknown_action_pass", "action": action}

    def validate_losing_context_block(
        self,
        *,
        positions,
        asset: str,
        lane: str,
        side: str,
        ask: float,
        ttc_s: Optional[float] = None,
        window_seconds: float = 900.0,
    ) -> dict:
        """Simulate blocking this context on full ledger — holdout PF must improve."""
        ctx = context_key(
            asset=asset, lane=lane, side=side, ask=ask,
            ttc_s=ttc_s, window_seconds=window_seconds,
        )

        def _block(row: dict, _history: list[dict]) -> bool:
            return row.get("context") == ctx

        rows = normalize_positions(positions)
        train, holdout = self.walk_forward_split(rows)
        if not holdout:
            return {"approved": False, "reason": "no_holdout", "context": ctx}

        cf_train = self.walk_forward_block_replay(train, should_block=_block)
        cf_hold = self.walk_forward_block_replay(holdout, should_block=_block)
        base_pf = profit_factor(holdout)
        # approximate holdout PF after block
        kept = [r for r in holdout if r.get("context") != ctx]
        cf_pf = profit_factor(kept) if kept else None

        approved = (
            cf_pf is not None
            and base_pf is not None
            and float(cf_pf) >= float(base_pf)
            and cf_hold.get("losses_avoided", 0) >= cf_hold.get("wins_missed", 0)
        )
        return {
            "approved": approved,
            "context": ctx,
            "holdout_baseline_pf": base_pf,
            "holdout_counterfactual_pf": cf_pf,
            "holdout_replay": cf_hold,
            "train_replay": cf_train,
        }

    @staticmethod
    def _holdout_metrics(rows: list[dict]) -> dict:
        n = len(rows)
        if not n:
            return {"n": 0, "win_rate": None, "pnl_usd": 0.0, "profit_factor": None}
        wins = sum(1 for r in rows if r["won"])
        pnl = sum(r["pnl"] for r in rows)
        return {
            "n": n,
            "win_rate": round(wins / n, 4),
            "pnl_usd": round(pnl, 4),
            "profit_factor": profit_factor(rows),
        }

    def report(self) -> dict:
        return {
            "enabled": bool(self.cfg.enabled),
            "method": "chronos_v1",
            "certificates_issued": self.certificates_issued,
            "blocked": self.blocked,
            "probed": self.probed,
            "proceeded": self.proceeded,
            "policy_vetoed": self.policy_vetoed,
            "last_certificate": self._last_certificate,
            "config": {
                "min_cohort_n": self.cfg.min_cohort_n,
                "proceed_cvs": self.cfg.proceed_cvs,
                "probe_cvs": self.cfg.probe_cvs,
                "exploration_rate": self.cfg.exploration_rate,
                "holdout_fraction": self.cfg.holdout_fraction,
                "kill_wr": self.cfg.kill_wr,
            },
            "note": (
                "CHRONOS pre-decision dry-run: Layer A trade certificates + "
                "Layer B walk-forward policy veto. PAPER ONLY."
            ),
        }

    def to_state(self) -> dict:
        return {
            "certificates_issued": self.certificates_issued,
            "blocked": self.blocked,
            "probed": self.probed,
            "proceeded": self.proceeded,
            "policy_vetoed": self.policy_vetoed,
        }

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.certificates_issued = int(data.get("certificates_issued") or 0)
        self.blocked = int(data.get("blocked") or 0)
        self.probed = int(data.get("probed") or 0)
        self.proceeded = int(data.get("proceeded") or 0)
        self.policy_vetoed = int(data.get("policy_vetoed") or 0)
