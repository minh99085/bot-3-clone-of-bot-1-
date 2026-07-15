"""Synthetic market generator for enhanced-misprice backtests.

Creates realistic mispricings:
  true_q ~ Beta(a, b)  (often extreme for crypto HF)
  model_q = clip(true_q + calibrated_noise)   → Brier roughly < 0.18
  market_p = clip(true_q + larger_noise/bias) → tradable dislocations
  outcome ~ Bernoulli(true_q)
"""

from __future__ import annotations

from typing import Iterator

import numpy as np

from models.config import EnhancedMispriceConfig, load_enhanced_config
from models.market import MarketSnapshot


def generate_synthetic_markets(
    n: int | None = None,
    *,
    config: EnhancedMispriceConfig | None = None,
    seed: int | None = None,
) -> list[MarketSnapshot]:
    cfg = config or load_enhanced_config()
    rng = np.random.default_rng(seed if seed is not None else cfg.synthetic_seed)
    n = int(n or cfg.synthetic_n_markets)

    # Stronger extremes so filtered q≥0.85 / q≤0.15 maps to high true_q
    true_q = np.empty(n)
    n_extreme = int(0.55 * n)
    n_mid = n - n_extreme
    true_q[: n_extreme // 2] = rng.beta(1.6, 18.0, size=n_extreme // 2)  # low
    true_q[n_extreme // 2 : n_extreme] = rng.beta(18.0, 1.6, size=n_extreme - n_extreme // 2)
    true_q[n_extreme:] = rng.beta(2.2, 2.2, size=n_mid)
    rng.shuffle(true_q)

    # Calibrated model: small noise; market: larger noise + lag bias
    model_noise = rng.normal(0.0, cfg.brier_noise_calibrated, size=n)
    market_noise = rng.normal(0.0, cfg.market_noise, size=n)
    lag_bias = rng.choice([-0.10, -0.05, 0.0, 0.05, 0.10], size=n, p=[0.15, 0.2, 0.3, 0.2, 0.15])

    model_q = np.clip(true_q + model_noise, 0.02, 0.98)
    market_p = np.clip(true_q + market_noise + lag_bias, 0.02, 0.98)

    outcomes = rng.random(n) < true_q
    liq = rng.lognormal(mean=8.5, sigma=0.9, size=n)  # ~$5k median
    vol = liq * rng.uniform(0.5, 3.0, size=n)
    ttr = rng.choice([300.0, 900.0, 3600.0], size=n, p=[0.45, 0.40, 0.15])

    markets: list[MarketSnapshot] = []
    for i in range(n):
        markets.append(
            MarketSnapshot(
                market_id=f"syn_{i:05d}",
                slug=f"synthetic-btc-{i:05d}",
                question=f"Synthetic market {i}",
                category="crypto",
                timeframe="5m" if ttr[i] <= 300 else ("15m" if ttr[i] <= 900 else "1h"),
                p=float(market_p[i]),
                q=float(model_q[i]),
                liquidity_usd=float(liq[i]),
                volume_24h=float(vol[i]),
                seconds_to_resolution=float(ttr[i]),
                true_q=float(true_q[i]),
                resolved_yes=bool(outcomes[i]),
                meta={"synthetic": True},
            )
        )
    return markets


def iter_events(markets: list[MarketSnapshot]) -> Iterator[MarketSnapshot]:
    """Event-driven view: one market resolution event at a time."""
    yield from markets


def estimate_brier(markets: list[MarketSnapshot]) -> float:
    """Brier score of model q vs resolved outcomes (calibration check)."""
    pairs = [(m.q, m.resolved_yes) for m in markets if m.resolved_yes is not None]
    if not pairs:
        return 1.0
    return float(np.mean([(q - (1.0 if y else 0.0)) ** 2 for q, y in pairs]))
