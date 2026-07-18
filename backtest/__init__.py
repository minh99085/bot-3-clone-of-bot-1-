"""backtest package — engine + honest data harnesses.

Imports are lazy (PEP 562) so dependency-light consumers — notably
scripts/pull_gamma_corpus.py, which needs only httpx — can import
``backtest.gamma_corpus`` on a box without numpy/scipy/pydantic installed.
"""

from typing import Any

__all__ = ["BacktestEngine", "SyntheticDataGenerator", "run_backtest"]


def __getattr__(name: str) -> Any:
    if name in ("BacktestEngine", "run_backtest"):
        from backtest import engine

        return getattr(engine, name)
    if name == "SyntheticDataGenerator":
        from backtest.synthetic_generator import SyntheticDataGenerator

        return SyntheticDataGenerator
    raise AttributeError(f"module 'backtest' has no attribute {name!r}")
