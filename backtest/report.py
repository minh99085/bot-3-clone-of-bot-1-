"""Backtest reporting: WR, buckets, PF, max DD, equity, calibration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from models.config import EnhancedMispriceConfig
from models.market import ClosedTrade


@dataclass
class BacktestReport:
    n_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    total_pnl: float = 0.0
    brier: float = 1.0
    wr_by_conviction: dict[str, float] = field(default_factory=dict)
    wr_by_edge: dict[str, float] = field(default_factory=dict)
    equity_curve: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "=== Enhanced Misprice Backtest ===",
            f"trades={self.n_trades}  WR={self.win_rate:.1%}  PF={self.profit_factor:.2f}",
            f"expectancy=${self.expectancy_usd:.2f}  PnL=${self.total_pnl:.2f}",
            f"maxDD={self.max_drawdown_pct:.1%}  model_Brier={self.brier:.3f}",
            "WR by conviction bucket:",
        ]
        for k, v in sorted(self.wr_by_conviction.items()):
            lines.append(f"  {k}: {v:.1%}")
        lines.append("WR by edge bucket:")
        for k, v in sorted(self.wr_by_edge.items()):
            lines.append(f"  {k}: {v:.1%}")
        for n in self.notes:
            lines.append(n)
        return "\n".join(lines)


def _wr(trades: Sequence[ClosedTrade]) -> float:
    if not trades:
        return 0.0
    return sum(1 for t in trades if t.won) / len(trades)


def _profit_factor(trades: Sequence[ClosedTrade]) -> float:
    gains = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    losses = sum(-t.pnl_usd for t in trades if t.pnl_usd < 0)
    if losses <= 1e-12:
        return 99.0 if gains > 0 else 0.0
    return gains / losses


def _max_dd(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for x in equity:
        peak = max(peak, x)
        if peak > 0:
            max_dd = max(max_dd, (peak - x) / peak)
    return float(max_dd)


def build_report(
    trades: Sequence[ClosedTrade],
    equity_curve: Sequence[float],
    *,
    brier: float,
    config: Optional[EnhancedMispriceConfig] = None,
) -> BacktestReport:
    _ = config
    conv_buckets = {"0.92-0.95": [], "0.95-0.97": [], "0.97-1.00": []}
    edge_buckets = {"0.06-0.10": [], "0.10-0.15": [], "0.15+": []}
    for t in trades:
        c = t.conviction_at_entry
        if c < 0.95:
            conv_buckets["0.92-0.95"].append(t)
        elif c < 0.97:
            conv_buckets["0.95-0.97"].append(t)
        else:
            conv_buckets["0.97-1.00"].append(t)
        e = t.edge_at_entry
        if e < 0.10:
            edge_buckets["0.06-0.10"].append(t)
        elif e < 0.15:
            edge_buckets["0.10-0.15"].append(t)
        else:
            edge_buckets["0.15+"].append(t)

    total = sum(t.pnl_usd for t in trades)
    n = len(trades)
    notes = []
    if brier >= 0.18:
        notes.append(
            f"NOTE: model Brier={brier:.3f} ≥ 0.18 — 80% WR not guaranteed; "
            "improve calibration before trusting live filters."
        )
    if n < 30:
        notes.append(f"NOTE: only {n} trades — increase synthetic_n_markets for power.")

    return BacktestReport(
        n_trades=n,
        win_rate=_wr(trades),
        profit_factor=_profit_factor(trades),
        expectancy_usd=(total / n) if n else 0.0,
        max_drawdown_pct=_max_dd(list(equity_curve)),
        total_pnl=total,
        brier=brier,
        wr_by_conviction={k: _wr(v) for k, v in conv_buckets.items() if v},
        wr_by_edge={k: _wr(v) for k, v in edge_buckets.items() if v},
        equity_curve=list(equity_curve),
        notes=notes,
    )


def calibration_points(
    q_list: Sequence[float], y_list: Sequence[bool], *, bins: int = 10
) -> list[tuple[float, float, int]]:
    """Return (bin_mid, empirical_freq, count) for a simple calibration plot."""
    qs = np.asarray(q_list, dtype=float)
    ys = np.asarray(y_list, dtype=float)
    if len(qs) == 0:
        return []
    edges = np.linspace(0.0, 1.0, bins + 1)
    out: list[tuple[float, float, int]] = []
    for i in range(bins):
        mask = (qs >= edges[i]) & (qs < edges[i + 1] if i < bins - 1 else qs <= edges[i + 1])
        if not mask.any():
            continue
        out.append(
            (
                float(0.5 * (edges[i] + edges[i + 1])),
                float(ys[mask].mean()),
                int(mask.sum()),
            )
        )
    return out
