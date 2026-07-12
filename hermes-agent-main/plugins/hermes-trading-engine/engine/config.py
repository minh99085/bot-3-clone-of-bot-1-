"""Central configuration for the Hermes Trading Engine (paper-trading).

PAPER-TRADING ONLY. Never sends real orders. `LIVE` mode (with safeguards) is
"armed simulation" — there is no real-exchange execution adapter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _data_dir() -> Path:
    raw = os.getenv("HTE_DATA_DIR", "/data")
    p = Path(raw)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = Path.home() / ".hermes" / "trading-engine"
        p.mkdir(parents=True, exist_ok=True)
    return p


AGGRESSIVENESS_PRESETS = {
    "cautious":   {"min_edge": 0.060, "max_stake": 0.020, "ev_threshold": 0.030, "kelly_fraction": 0.20, "stance": "cautious"},
    "balanced":   {"min_edge": 0.030, "max_stake": 0.040, "ev_threshold": 0.015, "kelly_fraction": 0.35, "stance": "balanced"},
    "aggressive": {"min_edge": 0.012, "max_stake": 0.060, "ev_threshold": 0.005, "kelly_fraction": 0.50, "stance": "aggressive"},
}


def _resolve_aggressiveness():
    name = os.getenv("HTE_AGGRESSIVENESS", "cautious").strip().lower()
    preset = AGGRESSIVENESS_PRESETS.get(name, AGGRESSIVENESS_PRESETS["balanced"])

    def pick(env_name, key):
        return _env_float(env_name, preset[key]) if os.getenv(env_name) is not None else preset[key]

    return (
        name if name in AGGRESSIVENESS_PRESETS else "cautious",
        pick("HTE_MIN_EDGE", "min_edge"), pick("HTE_MAX_STAKE_FRACTION", "max_stake"),
        pick("HTE_EV_THRESHOLD", "ev_threshold"), pick("HTE_KELLY_FRACTION", "kelly_fraction"),
        preset["stance"],
    )


_AGG, _MIN_EDGE, _MAX_STAKE, _EV_THRESH, _KELLY_FRAC, _STANCE = _resolve_aggressiveness()


@dataclass
class Settings:
    engine_name: str = os.getenv("HTE_ENGINE_NAME", "HermesTradingEngine")
    wallet_label: str = os.getenv("HTE_WALLET_LABEL", "0xce25e000000000000000000000000000000007fdc")

    # PAPER_STARTING_BALANCE (spec) overrides HTE_STARTING_BALANCE if set.
    starting_balance: float = _env_float("PAPER_STARTING_BALANCE",
                                         _env_float("HTE_STARTING_BALANCE", 100_000.0))

    aggressiveness: str = _AGG
    stance: str = _STANCE

    max_stake_fraction: float = _MAX_STAKE
    daily_loss_limit_fraction: float = _env_float("HTE_DAILY_LOSS_LIMIT", 0.10)
    max_exposure_fraction: float = _env_float("HTE_MAX_EXPOSURE", 0.50)
    min_edge: float = _MIN_EDGE
    ev_threshold: float = _EV_THRESH
    kelly_fraction: float = _KELLY_FRAC

    pulse_symbol: str = os.getenv("HTE_PULSE_SYMBOL", "BTCUSDT")
    pulse_round_seconds: int = _env_int("HTE_PULSE_ROUND_SECONDS", 300)
    pulse_vig: float = _env_float("HTE_PULSE_VIG", 0.04)
    # SAFE DEFAULT: autotrade OFF unless explicitly enabled (HTE_AUTOTRADE=1).
    autotrade_enabled: bool = os.getenv("HTE_AUTOTRADE", "0") not in ("0", "false", "False")
    # Polymarket-only PAPER training: when set, the legacy crypto/stock pulse
    # engine never opens (Polymarket paper trading runs in engine.training).
    polymarket_only_mode: bool = os.getenv("POLYMARKET_ONLY_MODE", "0") not in ("0", "false", "False", "")
    # PARALLEL BTC 5-min PULSE paper market: when set, the legacy pulse engine may
    # open the BTC 5-min PULSE market IN PARALLEL with Polymarket paper training —
    # PAPER ONLY (never live, never the legacy stock/Polymarket paths). Default OFF.
    # This is a paper-simulation flag; it never enables a live/real-money path.
    btc_pulse_paper_enabled: bool = os.getenv(
        "HTE_BTC_PULSE_PAPER_ENABLED", "0") not in ("0", "false", "False", "")
    disable_crypto_trading: bool = os.getenv("DISABLE_CRYPTO_TRADING", "0") not in ("0", "false", "False", "")
    disable_stock_trading: bool = os.getenv("DISABLE_STOCK_TRADING", "0") not in ("0", "false", "False", "")
    disable_arbitrage_trading: bool = os.getenv("DISABLE_ARBITRAGE_TRADING", "1") not in ("0", "false", "False", "")
    # Runtime mode is advisory metadata; the engine ALWAYS boots in PAPER and
    # there is no real-order execution adapter. Default kept safe.
    mode: str = os.getenv("HTE_MODE", "paper").strip().lower() or "paper"

    markov_lookback: int = _env_int("HTE_MARKOV_LOOKBACK", 400)
    montecarlo_paths: int = _env_int("HTE_MC_PATHS", 500)
    tick_seconds: float = _env_float("HTE_TICK_SECONDS", 2.0)

    crypto_symbols: list = field(default_factory=lambda: os.getenv(
        "HTE_CRYPTO_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(","))
    stock_symbols: list = field(default_factory=lambda: os.getenv(
        "HTE_STOCK_SYMBOLS", "AAPL,NVDA,TSLA,SPY").split(","))

    data_dir: Path = field(default_factory=_data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "trading_engine.sqlite3"


settings = Settings()
