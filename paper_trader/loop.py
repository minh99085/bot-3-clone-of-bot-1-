"""Async paper trading loop for enhanced misprice.

Every N minutes: pull Polymarket markets (Gamma) → build model q (pluggable;
default = CEX mispricing for BTC scope) → enhanced filters + Kelly → paper fills
→ early-exit management.

No private keys / on-chain signing. Paper lock enforced.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

import httpx

from models.config import EnhancedMispriceConfig, load_enhanced_config
from models.market import ClosedTrade, MarketSnapshot
from paper_trader.simulator import PaperSimulator
from risk.portfolio_risk import PortfolioRiskManager
from strategy.enhanced_misprice import rank_and_select
from strategy.bayesian import bayesian_conviction

logger = logging.getLogger(__name__)

# Pluggable model: (MarketSnapshot) -> q in [0,1]
ModelFn = Callable[[MarketSnapshot], float]


def placeholder_model(m: MarketSnapshot) -> float:
    """Simple statistical placeholder — shrink mid toward extremes lightly.

    Replace with LLM / polling / on-chain features. Hermes overnight loop
    uses CEX mispricing via ``enhance_from_hermes_mispricing`` instead.
    """
    mid = m.p
    return float(min(0.95, max(0.05, 0.5 + 0.85 * (mid - 0.5))))


async def fetch_live_markets(
    *,
    limit: int = 40,
    timeout: float = 15.0,
) -> list[MarketSnapshot]:
    """Pull active markets from Gamma (cached lightly by httpx)."""
    url = "https://gamma-api.polymarket.com/markets"
    params = {"active": "true", "closed": "false", "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            rows = r.json()
            if not isinstance(rows, list):
                rows = rows.get("markets") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("live fetch failed: %s", exc)
        return []

    out: list[MarketSnapshot] = []
    for i, row in enumerate(rows):
        try:
            prices = row.get("outcomePrices")
            if isinstance(prices, str):
                import json

                prices = json.loads(prices)
            p = float(prices[0]) if prices else float(row.get("lastTradePrice") or 0.5)
        except Exception:  # noqa: BLE001
            p = 0.5
        out.append(
            MarketSnapshot(
                market_id=str(row.get("id") or f"live_{i}"),
                slug=str(row.get("slug") or ""),
                question=str(row.get("question") or "")[:200],
                category="crypto" if "btc" in str(row.get("slug", "")).lower() else "default",
                p=min(0.98, max(0.02, p)),
                q=0.5,  # filled by model_fn
                liquidity_usd=float(row.get("liquidityNum") or 1000.0),
                volume_24h=float(row.get("volume24hr") or 0.0),
                seconds_to_resolution=900.0,
            )
        )
    return out


class EnhancedPaperLoop:
    """Async loop: discover → model → filter → size → paper trade → manage exits."""

    def __init__(
        self,
        config: Optional[EnhancedMispriceConfig] = None,
        *,
        model_fn: Optional[ModelFn] = None,
    ) -> None:
        self.cfg = config or load_enhanced_config()
        if not self.cfg.paper_only or os.environ.get("HERMES_PAPER_ONLY", "1") != "1":
            # Still force paper
            self.cfg.paper_only = True
        self.model_fn = model_fn or placeholder_model
        self.sim = PaperSimulator(self.cfg)
        self.risk = PortfolioRiskManager(self.cfg)
        self.risk.state.bankroll = self.sim.cash

    def _reprice_markets(self, markets: list[MarketSnapshot]) -> list[MarketSnapshot]:
        """Ensure each snapshot has latest Polymarket p and a fresh model q.

        Call before entry selection and before early-exit checks so decisions
        follow live CLOB prices, not beliefs frozen at loop start.
        """
        for m in markets:
            # p is already the latest fetch for this turn; recompute q from it
            m.q = float(self.model_fn(m))
            logger.debug(
                "reprice market_id=%s slug=%s live_p=%.4f fresh_q=%.4f",
                m.market_id,
                m.slug,
                m.p,
                m.q,
            )
        return markets

    async def turn(self) -> dict:
        markets = await fetch_live_markets(limit=30)
        if self.cfg.scope_btc_updown_only:
            from strategy.enhanced_misprice import filter_markets_by_scope

            markets = filter_markets_by_scope(markets) or markets[:0]

        # Live re-pricing before any entry decision
        markets = self._reprice_markets(markets)

        selected = rank_and_select(markets, risk_manager=self.risk, config=self.cfg)
        fills = 0
        for opp in selected:
            logger.info(
                "entry_decision q=%.4f p=%.4f edge=%.4f conviction=%.4f "
                "side=%s size=$%.2f slug=%s",
                opp.q,
                opp.p,
                opp.edge,
                opp.conviction,
                opp.side.value,
                opp.size_usd,
                opp.slug or opp.market_id,
            )
            fill = self.sim.open_position(opp)
            if fill is None:
                continue
            from models.market import OpenPosition

            pos = OpenPosition(
                position_id=fill.position_id,
                market_id=opp.market_id,
                slug=opp.slug,
                side=opp.side,
                entry_price=fill.fill_price,
                size_usd=fill.size_usd,
                shares=fill.shares,
                q_at_entry=opp.q,
                conviction_at_entry=opp.conviction,
                risk_unit=opp.risk_unit,
            )
            self.risk.record_open(pos)
            fills += 1

        exits = self._manage_early_exits(markets)
        return {
            "markets": len(markets),
            "selected": len(selected),
            "fills": fills,
            "early_exits": exits,
            "cash": self.sim.cash,
            "equity": self.sim.equity,
            "open": len(self.sim.open),
            "rolling_wr": self.risk.state.rolling_win_rate(self.cfg.rolling_wr_window),
            "dd": self.risk.state.drawdown_pct,
        }

    def _manage_early_exits(self, markets: list[MarketSnapshot]) -> int:
        by_id = {m.market_id: m for m in markets}
        n = 0
        for pos in list(self.risk.state.open_positions):
            m = by_id.get(pos.market_id)
            if m is None:
                continue
            # FIXED: Always recompute fresh q on every decision so we follow live Polymarket, not stale hallucinated beliefs.
            live_p = float(m.p)
            fresh_q = float(self.model_fn(m))
            m.q = fresh_q
            edge = abs(fresh_q - live_p)
            bayes = bayesian_conviction(
                fresh_q,
                live_p,
                self.cfg.n_eff.for_category(m.category),
                side=pos.side.value,
            )
            logger.info(
                "exit_consider q=%.4f p=%.4f edge=%.4f conviction=%.4f "
                "side=%s market=%s",
                fresh_q,
                live_p,
                edge,
                bayes.conviction,
                pos.side.value,
                pos.market_id,
            )
            if self.risk.should_early_exit(pos, bayes.conviction):
                pnl = -0.02 * pos.size_usd
                trade = ClosedTrade(
                    position_id=pos.position_id,
                    market_id=pos.market_id,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    exit_price=pos.entry_price,
                    size_usd=pos.size_usd,
                    pnl_usd=pnl,
                    won=False,
                    conviction_at_entry=pos.conviction_at_entry,
                    edge_at_entry=abs(pos.q_at_entry - pos.entry_price),
                    early_exit=True,
                )
                self.sim.close_position(trade)
                self.risk.record_close(trade)
                self.risk.state.bankroll = self.sim.cash
                n += 1
                logger.info(
                    "early_exit q=%.4f p=%.4f edge=%.4f conviction=%.4f market=%s",
                    fresh_q,
                    live_p,
                    edge,
                    bayes.conviction,
                    pos.market_id,
                )
        return n

    async def run_forever(self) -> None:
        logger.info(
            "Enhanced paper loop start interval=%ss bankroll=$%.0f",
            self.cfg.loop_interval_seconds,
            self.cfg.bankroll,
        )
        while True:
            try:
                stats = await self.turn()
                logger.info("turn %s", stats)
            except Exception:  # noqa: BLE001
                logger.exception("turn failed")
            await asyncio.sleep(self.cfg.loop_interval_seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    loop = EnhancedPaperLoop()
    asyncio.run(loop.run_forever())


if __name__ == "__main__":
    main()
