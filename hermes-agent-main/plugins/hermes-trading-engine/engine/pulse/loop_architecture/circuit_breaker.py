"""Hard token/capital/retry circuit breaker — clean process exit on violation (PAPER ONLY)."""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("pulse.loop_architecture.circuit_breaker")


@dataclass
class CircuitBreakerConfig:
    max_daily_token_usd: float = 50.0
    est_usd_per_api_call: float = 0.02
    max_api_calls_per_hour: int = 500
    min_on_hand_capital_usd: float = 50.0
    max_capital_drawdown_pct: float = 40.0
    starting_capital_usd: float = 500.0
    max_lane_retries: int = 5
    max_consecutive_errors: int = 20
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "CircuitBreakerConfig":
        def _f(k: str, default: float) -> float:
            try:
                return float(os.getenv(k, str(default)))
            except (TypeError, ValueError):
                return default

        def _i(k: str, default: int) -> int:
            try:
                return int(os.getenv(k, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            max_daily_token_usd=_f("PULSE_LOOP_MAX_DAILY_TOKEN_USD", 50.0),
            est_usd_per_api_call=_f("PULSE_LOOP_EST_USD_PER_CALL", 0.02),
            max_api_calls_per_hour=_i("PULSE_LOOP_MAX_API_CALLS_PER_HOUR", 500),
            min_on_hand_capital_usd=_f("PULSE_LOOP_MIN_ON_HAND_USD", 50.0),
            max_capital_drawdown_pct=_f("PULSE_LOOP_MAX_DRAWDOWN_PCT", 40.0),
            starting_capital_usd=_f("PULSE_STARTING_CAPITAL_USD", 500.0),
            max_lane_retries=_i("PULSE_LOOP_MAX_LANE_RETRIES", 5),
            max_consecutive_errors=_i("PULSE_LOOP_MAX_CONSECUTIVE_ERRORS", 20),
            enabled=str(os.getenv("PULSE_LOOP_CIRCUIT_BREAKER_ENABLED", "1")).lower()
            in ("1", "true", "yes"),
        )


@dataclass
class LoopCircuitBreaker:
    """Tracks spend, capital, and error ceilings; trips -> clean exit."""

    cfg: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig.from_env)
    tripped: bool = False
    trip_reason: str = ""
    api_calls_today: int = 0
    spent_today_usd: float = 0.0
    api_calls_hour: int = 0
    hour_bucket: int = 0
    consecutive_errors: int = 0
    lane_retries: dict = field(default_factory=dict)

    def record_api_call(self, *, now: Optional[float] = None) -> bool:
        """Record an API call; return False if budget exceeded (caller should trip)."""
        if not self.cfg.enabled:
            return True
        now = float(now if now is not None else time.time())
        day = int(now // 86400)
        hour = int(now // 3600)
        if hour != self.hour_bucket:
            self.hour_bucket = hour
            self.api_calls_hour = 0
        self.api_calls_hour += 1
        self.api_calls_today += 1
        self.spent_today_usd += self.cfg.est_usd_per_api_call
        if self.spent_today_usd > self.cfg.max_daily_token_usd:
            self._trip(f"daily_token_budget_exceeded (${self.spent_today_usd:.2f})")
            return False
        if self.api_calls_hour > self.cfg.max_api_calls_per_hour:
            self._trip(f"hourly_api_call_ceiling ({self.api_calls_hour})")
            return False
        return True

    def check_capital(self, on_hand_usd: float, starting_usd: Optional[float] = None) -> bool:
        start = float(starting_usd if starting_usd is not None else self.cfg.starting_capital_usd)
        if on_hand_usd < self.cfg.min_on_hand_capital_usd:
            self._trip(f"on_hand_below_floor (${on_hand_usd:.2f} < ${self.cfg.min_on_hand_capital_usd})")
            return False
        dd_pct = 100.0 * (start - on_hand_usd) / max(1.0, start)
        if dd_pct > self.cfg.max_capital_drawdown_pct:
            self._trip(f"capital_drawdown_{dd_pct:.1f}pct")
            return False
        return True

    def record_lane_error(self, lane: str) -> None:
        self.consecutive_errors += 1
        self.lane_retries[lane] = int(self.lane_retries.get(lane, 0)) + 1
        if self.consecutive_errors >= self.cfg.max_consecutive_errors:
            self._trip(f"consecutive_errors_{self.consecutive_errors}")
        elif self.lane_retries.get(lane, 0) >= self.cfg.max_lane_retries:
            self._trip(f"lane_retry_ceiling_{lane}")

    def record_lane_success(self) -> None:
        self.consecutive_errors = 0

    def _trip(self, reason: str) -> None:
        if self.tripped:
            return
        self.tripped = True
        self.trip_reason = reason
        logger.critical("LOOP CIRCUIT BREAKER TRIPPED: %s — clean exit", reason)

    def exit_if_tripped(self) -> None:
        """Clean process exit when a hard cap is violated."""
        if self.tripped:
            logger.critical("Exiting process: circuit_breaker=%s", self.trip_reason)
            sys.exit(2)

    def report(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "tripped": self.tripped,
            "trip_reason": self.trip_reason or None,
            "spent_today_usd": round(self.spent_today_usd, 4),
            "api_calls_today": self.api_calls_today,
            "api_calls_hour": self.api_calls_hour,
            "consecutive_errors": self.consecutive_errors,
            "lane_retries": dict(self.lane_retries),
        }
