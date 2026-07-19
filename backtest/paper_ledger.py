"""Real out-of-sample corpus — the paper fleet's own trade ledger.

The Polymarket Gamma API does not retain resolved 5m/15m crypto up/down
markets (proven: exact-slug fetch returns empty once a window passes), so a
historical pull is impossible for this market type. The genuine record of
"real, unseen markets after costs" is what the live paper fleet already logs:

    data/paper/<instance>/trade_ledger.jsonl   settlement events (outcome, pnl)
    data/paper/<instance>/pretrade_decisions.jsonl   model q at decision time
    reports/*/trades.json                      periodic settled-trade bundles

This module normalizes those into ``RealTrade`` records and computes an
HONEST report: win rate with a Wilson interval, profit factor, Brier,
log-loss, calibration error, and max drawdown — all after costs — and
refuses to call a few dozen trades evidence of edge.

No fabrication: model q is taken from the pretrade log when present, else
reconstructed from the logged edge; never invented from the outcome.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^(btc|eth|sol)-updown-(5m|15m)-(\d+)$")
_CEX_RE = re.compile(r"entry_cex=([0-9.]+)\s+exit_cex=([0-9.]+)")
MIN_TRADES_FOR_EDGE = 100  # below this, no go/no-go call is honest


def parse_slug_window(slug: str) -> Optional[tuple[str, str, int]]:
    m = SLUG_RE.match((slug or "").strip().lower())
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def parse_cex_notes(notes: str) -> tuple[Optional[float], Optional[float]]:
    m = _CEX_RE.search(notes or "")
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


@dataclass
class RealTrade:
    """One settled real paper trade (after costs)."""

    slug: str
    asset: str
    timeframe: str
    window_ts: int
    settled_at: str
    direction: str  # "UP" | "DOWN"
    p_side: float  # price paid for the chosen side
    won: bool
    pnl_usd: float
    size_usd: float
    instance_id: str = ""
    q_up: Optional[float] = None  # model P(up); None until known
    entry_cex: Optional[float] = None
    exit_cex: Optional[float] = None
    source: str = "ledger"

    @property
    def outcome_up(self) -> bool:
        # direction UP won  → up; DOWN won → down; etc.
        return (self.direction.upper() == "UP") == self.won

    @property
    def p_up(self) -> float:
        return self.p_side if self.direction.upper() == "UP" else 1.0 - self.p_side


def _dir_of(rec: dict[str, Any]) -> str:
    d = str(rec.get("direction") or rec.get("side") or "").upper()
    return "UP" if d in ("UP", "YES") else "DOWN"


def _q_up_from_edge(direction: str, p_side: float, edge: Optional[float]) -> Optional[float]:
    """Recover model P(up) from the chosen side price + |q-p| edge.

    The side is chosen toward q, so q_side = p_side + edge; map to P(up).
    """
    if edge is None:
        return None
    q_side = min(1.0, max(0.0, p_side + float(edge)))
    return q_side if direction.upper() == "UP" else 1.0 - q_side


def _trade_from_record(rec: dict[str, Any]) -> Optional[RealTrade]:
    slug = str(rec.get("slug") or "")
    win = parse_slug_window(slug)
    if win is None:
        return None
    asset, tf, ts = win
    if rec.get("won") is None:
        return None
    p_side = rec.get("entry_price")
    if p_side is None:
        return None
    direction = _dir_of(rec)
    edge = rec.get("enhanced_edge")
    if edge is None:
        edge = (rec.get("meta") or {}).get("enhanced_edge")
    entry_cex, exit_cex = parse_cex_notes(str(rec.get("notes") or ""))
    return RealTrade(
        slug=slug,
        asset=asset,
        timeframe=tf,
        window_ts=ts,
        settled_at=str(rec.get("settled_at") or rec.get("ts") or ""),
        direction=direction,
        p_side=float(p_side),
        won=bool(rec.get("won")),
        pnl_usd=float(rec.get("pnl_usd") or 0.0),
        size_usd=float(rec.get("size_usd") or 0.0),
        instance_id=str(rec.get("instance_id") or ""),
        q_up=_q_up_from_edge(direction, float(p_side), edge),
        entry_cex=entry_cex,
        exit_cex=exit_cex,
        source=str(rec.get("_source") or "ledger"),
    )


def _iter_json_records(path: Path) -> Iterable[dict[str, Any]]:
    """Yield records from a .jsonl ledger or a .json array bundle."""
    if not path.is_file():
        return
    if path.suffix == ".jsonl":
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("paper_ledger: bad jsonl line in %s", path)
    else:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            logger.warning("paper_ledger: unreadable json %s", path)
            return
        if isinstance(data, list):
            yield from (r for r in data if isinstance(r, dict))


def load_pretrade_q(paths: Sequence[Path]) -> dict[str, float]:
    """signal_id/slug → model P(up) from pretrade_decisions.jsonl, if available."""
    q_by_key: dict[str, float] = {}
    for path in paths:
        for rec in _iter_json_records(path):
            q = rec.get("q") or rec.get("model_q") or rec.get("q_up")
            if q is None:
                continue
            key = str(rec.get("signal_id") or rec.get("slug") or "")
            if key:
                q_by_key[key] = float(q)
    return q_by_key


def load_trades(
    paths: Sequence[Path | str],
    *,
    pretrade_paths: Sequence[Path | str] = (),
    only_settlements: bool = True,
) -> list[RealTrade]:
    """Load + normalize settled real trades from ledgers/bundles.

    ``only_settlements`` keeps just resolved events (event=='settlement' or a
    bundle row with 'won'); fills/opens are ignored.
    """
    q_join = load_pretrade_q([Path(p) for p in pretrade_paths]) if pretrade_paths else {}
    out: list[RealTrade] = []
    seen: set[tuple[str, str]] = set()
    for p in paths:
        path = Path(p)
        for rec in _iter_json_records(path):
            if only_settlements and path.suffix == ".jsonl":
                if rec.get("event") not in ("settlement", None) and rec.get("won") is None:
                    continue
            t = _trade_from_record(rec)
            if t is None:
                continue
            dedup = (t.slug, t.settled_at or str(rec.get("signal_id") or ""))
            if dedup in seen:
                continue
            seen.add(dedup)
            # Prefer a real pretrade q over the edge reconstruction
            key = str(rec.get("signal_id") or t.slug)
            if key in q_join:
                t.q_up = q_join[key]
            out.append(t)
    out.sort(key=lambda t: (t.window_ts, t.slug))
    return out


# --------------------------------------------------------------------------
# Honest report (Task 8)
# --------------------------------------------------------------------------

def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _max_drawdown(pnls: Sequence[float], bankroll: float) -> float:
    eq = bankroll
    peak = bankroll
    mdd = 0.0
    for pnl in pnls:
        eq += pnl
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    return mdd


@dataclass
class RealReport:
    n_trades: int = 0
    n_wins: int = 0
    win_rate: float = 0.0
    wilson_lo: float = 0.0
    wilson_hi: float = 1.0
    profit_factor: float = 0.0
    expectancy_usd: float = 0.0
    total_pnl: float = 0.0
    roi: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_entry_price: float = 0.0  # ~ breakeven win rate
    brier: Optional[float] = None
    log_loss: Optional[float] = None
    calibration_error: Optional[float] = None
    market_brier: Optional[float] = None
    n_with_q: int = 0
    sufficient_n: bool = False
    by_series: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [
            "=== REAL OUT-OF-SAMPLE REPORT (paper fleet, after costs) ===",
        ]
        if not self.sufficient_n:
            lines += [
                f"*** INSUFFICIENT DATA: {self.n_trades} settled trades "
                f"(need >= {MIN_TRADES_FOR_EDGE} for a go/no-go call). ***",
                "*** Numbers below are descriptive only, NOT evidence of edge. ***",
            ]
        lines += [
            "",
            f"Settled trades:   {self.n_trades}",
            f"Win rate:         {self.win_rate:.1%}  (95% CI "
            f"{self.wilson_lo:.1%}–{self.wilson_hi:.1%})",
            f"Breakeven WR:     {self.avg_entry_price:.1%}  (avg entry price)",
            f"Profit factor:    {self.profit_factor:.2f}",
            f"Expectancy/trade: ${self.expectancy_usd:.2f}",
            f"Total PnL:        ${self.total_pnl:.2f}  (ROI {self.roi:.1%})",
            f"Max drawdown:     {self.max_drawdown_pct:.1%}",
        ]
        if self.brier is not None:
            lines += [
                f"Model Brier:      {self.brier:.4f}  (market {self.market_brier:.4f}) "
                f"[n_with_q={self.n_with_q}]",
                f"Model log-loss:   {self.log_loss:.4f}",
                f"Calibration err:  {self.calibration_error:.4f}",
            ]
        else:
            lines.append("Model Brier/log-loss/calibration: n/a (no q on these trades)")
        if self.by_series:
            lines.append(f"By series:        {self.by_series}")
        for n in self.notes:
            lines.append(f"NOTE: {n}")
        return "\n".join(lines)


def build_real_report(
    trades: Sequence[RealTrade], *, bankroll: float = 2000.0
) -> RealReport:
    r = RealReport()
    r.n_trades = len(trades)
    if not trades:
        r.notes.append("no settled real trades found")
        return r
    r.n_wins = sum(1 for t in trades if t.won)
    r.win_rate = r.n_wins / r.n_trades
    r.wilson_lo, r.wilson_hi = _wilson(r.n_wins, r.n_trades)
    gains = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    losses = sum(-t.pnl_usd for t in trades if t.pnl_usd < 0)
    r.profit_factor = (gains / losses) if losses > 1e-9 else (float("inf") if gains > 0 else 0.0)
    r.total_pnl = sum(t.pnl_usd for t in trades)
    r.expectancy_usd = r.total_pnl / r.n_trades
    fleet_bankroll = bankroll * max(1, len({t.instance_id for t in trades if t.instance_id}))
    r.roi = r.total_pnl / fleet_bankroll if fleet_bankroll else 0.0
    r.max_drawdown_pct = _max_drawdown([t.pnl_usd for t in trades], fleet_bankroll)
    r.avg_entry_price = sum(t.p_side for t in trades) / r.n_trades
    for t in trades:
        key = f"{t.asset}_{t.timeframe}"
        r.by_series[key] = r.by_series.get(key, 0) + 1

    withq = [t for t in trades if t.q_up is not None]
    r.n_with_q = len(withq)
    MIN_Q_FOR_CALIB = 20
    if r.n_with_q >= MIN_Q_FOR_CALIB and r.n_with_q >= r.n_trades // 2:
        def clip(x: float) -> float:
            return min(1 - 1e-6, max(1e-6, x))

        ys = [1.0 if t.outcome_up else 0.0 for t in withq]
        qs = [clip(float(t.q_up)) for t in withq]
        ps = [clip(t.p_up) for t in withq]
        r.brier = sum((q - y) ** 2 for q, y in zip(qs, ys)) / len(ys)
        r.market_brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys)
        r.log_loss = -sum(
            y * math.log(q) + (1 - y) * math.log(1 - q) for q, y in zip(qs, ys)
        ) / len(ys)
        r.calibration_error = _ece(qs, ys)
    elif r.n_with_q < MIN_Q_FOR_CALIB:
        r.notes.append(
            f"calibration skipped: only {r.n_with_q} trades with a model q "
            f"(need >= {MIN_Q_FOR_CALIB} for a stable reliability estimate)"
        )
    else:
        r.notes.append(
            f"calibration skipped: model q present on only {r.n_with_q}/{r.n_trades} trades"
        )

    r.sufficient_n = r.n_trades >= MIN_TRADES_FOR_EDGE
    if not r.sufficient_n:
        r.notes.append(
            f"{r.n_trades} trades < {MIN_TRADES_FOR_EDGE}: no go/no-go call is statistically honest"
        )
    return r


def _ece(qs: Sequence[float], ys: Sequence[float], bins: int = 10) -> float:
    """Expected calibration error over equal-width probability bins."""
    n = len(qs)
    if n == 0:
        return 0.0
    tot = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, q in enumerate(qs) if (q >= lo and (q < hi or (b == bins - 1 and q <= hi)))]
        if not idx:
            continue
        conf = sum(qs[i] for i in idx) / len(idx)
        acc = sum(ys[i] for i in idx) / len(idx)
        tot += (len(idx) / n) * abs(conf - acc)
    return tot


def full_report(
    trades: Sequence[RealTrade],
    *,
    bankroll: float = 2000.0,
    run_barrier: bool = True,
) -> str:
    """Honest after-cost report + (optionally) the barrier-vs-market eval.

    The barrier section needs CEX price lookups (window-open + intra-window
    klines) and so only runs where those are reachable (VPS / allowlisted
    session). It degrades gracefully — excluded trades are counted, and it
    still prints whatever it could evaluate.
    """
    out = [build_real_report(trades, bankroll=bankroll).text()]
    if run_barrier:
        try:
            from backtest.barrier_eval import (
                BarrierEvalConfig,
                evaluate_barrier,
            )
            from connectors.cex_realtime import price_at_timestamp

            def _open_fn(asset: str, ts: int) -> float:
                return float(price_at_timestamp(asset, int(ts)) or 0.0)

            def _path_fn(asset: str, ts0: int, ts1: int):
                # 1-min klines across the window for realized σ.
                pts = []
                for k in range(0, max(1, (ts1 - ts0) // 60) + 1):
                    px = price_at_timestamp(asset, ts0 + k * 60)
                    if px and px > 0:
                        pts.append((ts0 + k * 60, float(px)))
                return pts

            rep = evaluate_barrier(
                trades, open_price_fn=_open_fn, window_path_fn=_path_fn,
                cfg=BarrierEvalConfig(),
            )
            out.append("")
            out.append(rep.text())
        except Exception as exc:  # noqa: BLE001
            out.append(f"\n[barrier eval skipped: {exc}]")
    return "\n".join(out)


def default_ledger_paths(root: Path | str = "data/paper") -> list[Path]:
    root = Path(root)
    return sorted(root.glob("*/trade_ledger.jsonl"))


def default_pretrade_paths(root: Path | str = "data/paper") -> list[Path]:
    root = Path(root)
    return sorted(root.glob("*/pretrade_decisions.jsonl"))
