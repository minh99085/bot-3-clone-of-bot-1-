"""Portfolio risk budgeting + dynamic drawdown / win-rate guards.

Per-bet risk unit ≈ (position_size * sqrt(p * (1 - p))) ** 2
Aggregate open risk units ≤ risk_budget (default 0.20).

Guards:
  if peak-to-trough DD > 8% OR rolling_20 WR < 75%:
      raise conviction threshold → 0.95 and set kappa → 0.20
  early exit if live conviction < 0.35
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from models.config import EnhancedMispriceConfig, load_enhanced_config
from models.market import ClosedTrade, OpenPosition, TradeOpportunity


def risk_unit(
    position_size: float,
    p: float,
    *,
    bankroll: float | None = None,
) -> float:
    """Approximate variance-style risk unit for a binary contract.

    Product formula: (position_size * sqrt(p * (1 - p))) ** 2

    ``position_size`` may be passed as a bankroll *fraction* or as USD.
    When ``bankroll`` is provided we normalize USD → fraction so that a
    portfolio ``risk_budget`` of ~0.20 is dimensionally meaningful
    (otherwise dollar-sized units are ~10^6 and nothing ever fits).
    """
    p_c = min(1.0 - 1e-9, max(1e-9, float(p)))
    size = float(position_size)
    if bankroll is not None and bankroll > 0 and size > 1.0:
        # Heuristic: values > 1 look like USD notionals
        size = size / float(bankroll)
    size = max(0.0, min(1.0, size))
    return (size * math.sqrt(p_c * (1.0 - p_c))) ** 2


def _is_crypto(meta: Optional[dict], slug: str = "") -> bool:
    cat = str((meta or {}).get("category") or "").lower()
    if cat:
        return cat == "crypto"
    return "updown" in (slug or "").lower()


def _direction(side) -> str:
    """Collapse YES/UP → 'up', NO/DOWN → 'down' for correlation grouping."""
    s = getattr(side, "value", side)
    return "up" if str(s).upper() in ("YES", "UP") else "down"


@dataclass
class GuardState:
    """Dynamic risk / selectivity regime."""

    kappa: float
    min_conviction: float
    drawdown_pct: float
    rolling_wr: float
    guard_active: bool
    reason: str = ""


@dataclass
class PortfolioRiskState:
    bankroll: float
    peak_bankroll: float
    open_positions: list[OpenPosition] = field(default_factory=list)
    recent_results: list[bool] = field(default_factory=list)  # True=win
    closed: list[ClosedTrade] = field(default_factory=list)

    @property
    def equity(self) -> float:
        # Mark open at cost for conservative DD (paper MTM can replace)
        return self.bankroll + sum(p.size_usd for p in self.open_positions)

    @property
    def drawdown_pct(self) -> float:
        peak = max(self.peak_bankroll, self.equity, 1e-9)
        return max(0.0, (peak - self.equity) / peak)

    def rolling_win_rate(self, window: int = 20) -> float:
        if not self.recent_results:
            return 1.0  # cold start: do not trip guard
        tail = self.recent_results[-window:]
        return sum(1 for x in tail if x) / len(tail)

    def open_risk_units(self) -> float:
        return sum(float(p.risk_unit) for p in self.open_positions)

    def crypto_dir_risk_units(self, direction: str) -> float:
        """Open crypto risk units on one direction (correlation factor)."""
        return sum(
            float(p.risk_unit)
            for p in self.open_positions
            if _is_crypto(getattr(p, "meta", None), getattr(p, "slug", ""))
            and _direction(p.side) == direction
        )


class PortfolioRiskManager:
    """Enforces aggregate risk budget + DD/WR guards + early-exit policy."""

    def __init__(self, config: Optional[EnhancedMispriceConfig] = None) -> None:
        self.cfg = config or load_enhanced_config()
        self.state = PortfolioRiskState(
            bankroll=self.cfg.bankroll,
            peak_bankroll=self.cfg.bankroll,
        )

    def evaluate_guards(self) -> GuardState:
        dd = self.state.drawdown_pct
        wr = self.state.rolling_win_rate(self.cfg.rolling_wr_window)
        trip_dd = dd > self.cfg.dd_guard_pct
        trip_wr = (
            len(self.state.recent_results) >= self.cfg.rolling_wr_window
            and wr < self.cfg.rolling_wr_floor
        )
        if trip_dd or trip_wr:
            reasons = []
            if trip_dd:
                reasons.append(f"DD={dd:.2%}>{self.cfg.dd_guard_pct:.0%}")
            if trip_wr:
                reasons.append(
                    f"WR{self.cfg.rolling_wr_window}={wr:.2%}<{self.cfg.rolling_wr_floor:.0%}"
                )
            return GuardState(
                kappa=self.cfg.kappa_guard,
                min_conviction=self.cfg.min_conviction_guard,
                drawdown_pct=dd,
                rolling_wr=wr,
                guard_active=True,
                reason="; ".join(reasons),
            )
        return GuardState(
            kappa=self.cfg.kappa_base,
            min_conviction=self.cfg.min_conviction,
            drawdown_pct=dd,
            rolling_wr=wr,
            guard_active=False,
            reason="nominal",
        )

    def can_add(self, opp: TradeOpportunity) -> tuple[bool, str]:
        """Check whether adding this opportunity stays inside risk_budget."""
        if self.state.drawdown_pct >= self.cfg.max_drawdown_hard_pct:
            return False, f"hard_dd={self.state.drawdown_pct:.2%}"
        projected = self.state.open_risk_units() + opp.risk_unit
        if projected > self.cfg.risk_budget + 1e-12:
            return (
                False,
                f"risk_units={projected:.6f}>{self.cfg.risk_budget}",
            )
        # Correlation-aware cap: same-direction crypto is one risk factor.
        if _is_crypto(opp.meta, opp.slug):
            d = _direction(opp.side)
            dir_proj = self.state.crypto_dir_risk_units(d) + opp.risk_unit
            if dir_proj > self.cfg.crypto_dir_risk_budget + 1e-12:
                return (
                    False,
                    f"crypto_dir_{d}_risk={dir_proj:.6f}>{self.cfg.crypto_dir_risk_budget}",
                )
        # 1e-6 USD tolerance avoids rejecting fills that land exactly on the cap
        # after float rounding (e.g. 0.09 * 1820 ≈ 163.8).
        if opp.size_usd > self.cfg.max_single_market_pct * self.state.bankroll + 1e-6:
            return False, "size_above_single_market_cap"
        if opp.size_usd > self.state.bankroll + 1e-9:
            return False, "insufficient_cash"
        return True, "ok"

    def select_within_budget(
        self, opportunities: Sequence[TradeOpportunity]
    ) -> list[TradeOpportunity]:
        """Greedy: take highest conviction_score that fits risk budget."""
        chosen: list[TradeOpportunity] = []
        # Work on a copy of open risk so we can simulate fills
        used = self.state.open_risk_units()
        cash = self.state.bankroll
        equity = cash + sum(p.size_usd for p in self.state.open_positions)
        # Same-direction crypto exposure is one correlated risk factor.
        dir_used = {
            "up": self.state.crypto_dir_risk_units("up"),
            "down": self.state.crypto_dir_risk_units("down"),
        }
        for opp in sorted(opportunities, key=lambda o: o.conviction_score, reverse=True):
            if not opp.passes_hard_filter or opp.size_usd <= 0:
                continue
            if used + opp.risk_unit > self.cfg.risk_budget + 1e-12:
                continue
            is_crypto = _is_crypto(opp.meta, opp.slug)
            d = _direction(opp.side)
            if is_crypto and dir_used[d] + opp.risk_unit > self.cfg.crypto_dir_risk_budget + 1e-12:
                continue  # correlation cap: throttle same-way crypto basket
            if opp.size_usd > cash + 1e-9:
                continue
            if opp.size_usd > self.cfg.max_single_market_pct * equity + 1e-6:
                continue
            chosen.append(opp)
            used += opp.risk_unit
            if is_crypto:
                dir_used[d] += opp.risk_unit
            cash -= opp.size_usd
            equity = cash + sum(p.size_usd for p in self.state.open_positions) + sum(
                c.size_usd for c in chosen
            )
        return chosen

    def should_early_exit(
        self, position: OpenPosition, live_conviction: float
    ) -> bool:
        """Close if live-updated conviction drops below early_exit threshold."""
        return live_conviction < self.cfg.early_exit_conviction

    def record_open(self, pos: OpenPosition) -> None:
        self.state.bankroll -= pos.size_usd
        self.state.open_positions.append(pos)
        self.state.peak_bankroll = max(self.state.peak_bankroll, self.state.equity)

    def record_close(self, trade: ClosedTrade) -> None:
        self.state.open_positions = [
            p for p in self.state.open_positions if p.position_id != trade.position_id
        ]
        # Return stake + PnL (stake was removed at open)
        self.state.bankroll += trade.size_usd + trade.pnl_usd
        self.state.recent_results.append(trade.won)
        self.state.closed.append(trade)
        self.state.peak_bankroll = max(self.state.peak_bankroll, self.state.equity)

    def mark_to_market(self, mids: dict[str, float]) -> float:
        """Optional MTM equity using current mid for the held side."""
        mtm = self.state.bankroll
        for p in self.state.open_positions:
            mid = mids.get(p.market_id)
            if mid is None:
                mtm += p.size_usd
                continue
            # PnL mark: shares * mid - size  (shares = size/entry)
            shares = p.size_usd / max(p.entry_price, 1e-9)
            mtm += shares * mid
        return mtm
