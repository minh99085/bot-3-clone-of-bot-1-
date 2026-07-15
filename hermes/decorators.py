"""Loop Engineering primitives: @loop, @goal, @checker.

Inspired by Addy Osmani's five moves / six parts and Roan's quant trading
patterns. These decorators make cadence and stopping conditions first-class
in code so automations trigger *skills*, not walls of prompt.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _parse_interval(interval: str | float | int) -> float:
    """Parse '30s', '1m', '5m', '1h' or raw seconds into float seconds."""
    if isinstance(interval, (int, float)):
        return float(interval)
    s = interval.strip().lower()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000.0
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60.0
    if s.endswith("h"):
        return float(s[:-1]) * 3600.0
    return float(s)


@dataclass
class LoopMeta:
    interval: float
    name: str
    enabled: bool = True
    last_run: Optional[datetime] = None
    run_count: int = 0
    errors: int = 0


@dataclass
class GoalMeta:
    condition: str
    checker: Optional[Callable[..., bool]] = None
    max_turns: int = 50
    max_hours: float = 48.0
    description: str = ""


@dataclass
class CheckerMeta:
    """Marks a function as a maker-checker evaluator (not the generator)."""

    name: str
    model_hint: str = "stronger"
    assume_broken_until_proven: bool = True
    criteria: list[str] = field(default_factory=list)


_REGISTRY: dict[str, LoopMeta] = {}
_GOAL_REGISTRY: dict[str, GoalMeta] = {}
_CHECKER_REGISTRY: dict[str, CheckerMeta] = {}


def loop(
    interval: str | float | int = "5m",
    *,
    name: Optional[str] = None,
    enabled: bool = True,
) -> Callable:
    """Schedule a function to run on a cadence (Automation part).

    Example::

        @loop(interval="5m")
        def discovery_tick():
            ...

        @loop(interval="30s")
        async def risk_monitor_tick():
            ...
    """

    seconds = _parse_interval(interval)

    def decorator(fn: Callable) -> Callable:
        meta_name = name or fn.__name__
        meta = LoopMeta(interval=seconds, name=meta_name, enabled=enabled)
        _REGISTRY[meta_name] = meta

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                meta.last_run = datetime.now(timezone.utc)
                meta.run_count += 1
                try:
                    return await fn(*args, **kwargs)
                except Exception:
                    meta.errors += 1
                    logger.exception("loop %s failed", meta_name)
                    raise

            async_wrapper._loop_meta = meta  # type: ignore[attr-defined]
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            meta.last_run = datetime.now(timezone.utc)
            meta.run_count += 1
            try:
                return fn(*args, **kwargs)
            except Exception:
                meta.errors += 1
                logger.exception("loop %s failed", meta_name)
                raise

        sync_wrapper._loop_meta = meta  # type: ignore[attr-defined]
        return sync_wrapper

    return decorator


def goal(
    condition: str,
    *,
    checker: Optional[Callable[..., bool]] = None,
    max_turns: int = 50,
    max_hours: float = 48.0,
    description: str = "",
) -> Callable:
    """High-conviction work: iterate until an external checker says stop.

    The checker must be a *different* callable than the body (maker-checker).
    Example::

        @goal(
            "3+ verified high-edge signals OR 48h pass with no action",
            checker=external_goal_checker,
            max_hours=48,
        )
        def research_market(market_id: str):
            ...
    """

    def decorator(fn: Callable) -> Callable:
        meta = GoalMeta(
            condition=condition,
            checker=checker,
            max_turns=max_turns,
            max_hours=max_hours,
            description=description or condition,
        )
        _GOAL_REGISTRY[fn.__name__] = meta

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            turn = 0
            last_result: Any = None
            while turn < meta.max_turns:
                turn += 1
                elapsed_h = (time.monotonic() - started) / 3600.0
                if elapsed_h >= meta.max_hours:
                    logger.info("goal %s: max_hours reached", fn.__name__)
                    break
                last_result = fn(*args, **kwargs)
                if meta.checker is not None:
                    try:
                        done = meta.checker(last_result, *args, **kwargs)
                    except TypeError:
                        done = meta.checker(last_result)
                    if done:
                        logger.info(
                            "goal %s: condition met on turn %d", fn.__name__, turn
                        )
                        break
                else:
                    # Without an external checker, run once (safe default).
                    break
            return last_result

        wrapper._goal_meta = meta  # type: ignore[attr-defined]
        return wrapper

    return decorator


def checker(
    name: Optional[str] = None,
    *,
    model_hint: str = "stronger",
    assume_broken_until_proven: bool = True,
    criteria: Optional[list[str]] = None,
) -> Callable:
    """Mark a function as an evaluator (the thing that can say NO).

    Default stance: assume the signal/work is broken until proven otherwise.
    """

    def decorator(fn: Callable) -> Callable:
        meta = CheckerMeta(
            name=name or fn.__name__,
            model_hint=model_hint,
            assume_broken_until_proven=assume_broken_until_proven,
            criteria=criteria or [],
        )
        _CHECKER_REGISTRY[meta.name] = meta
        fn._checker_meta = meta  # type: ignore[attr-defined]
        return fn

    return decorator


def get_loops() -> dict[str, LoopMeta]:
    return dict(_REGISTRY)


def get_goals() -> dict[str, GoalMeta]:
    return dict(_GOAL_REGISTRY)


def get_checkers() -> dict[str, CheckerMeta]:
    return dict(_CHECKER_REGISTRY)


async def run_loop_forever(fn: Callable, *, stop_event: Optional[asyncio.Event] = None) -> None:
    """Run a @loop-decorated async/sync function forever on its cadence."""
    meta: LoopMeta = getattr(fn, "_loop_meta", None)
    if meta is None:
        raise ValueError(f"{fn} is not decorated with @loop")
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        if meta.enabled:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                await asyncio.to_thread(fn)
        try:
            await asyncio.wait_for(stop.wait(), timeout=meta.interval)
        except asyncio.TimeoutError:
            continue
