"""Retry with exponential backoff and jitter for clearinghouse / gateway calls."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class TransientGatewayError(Exception):
    """A failure worth retrying (timeouts, 502/503/504, connection resets)."""


class GatewayTimeout(TransientGatewayError):
    pass


class GatewayUnavailable(TransientGatewayError):
    pass


class RetryExhausted(Exception):
    def __init__(self, attempts: int, last_error: BaseException):
        super().__init__(f"Gave up after {attempts} attempts: {last_error!r}")
        self.attempts = attempts
        self.last_error = last_error


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    multiplier: float = 2.0
    max_delay: float = 8.0
    jitter: float = 0.1  # +/- fraction of the computed delay

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay after the given failed attempt (1-based): base * multiplier^(attempt-1), capped."""
        delay = min(self.max_delay, self.base_delay * self.multiplier ** (attempt - 1))
        if self.jitter and rng is not None:
            delay *= 1 + rng.uniform(-self.jitter, self.jitter)
        return max(0.0, delay)


@dataclass(frozen=True)
class RetryOutcome(Generic[T]):
    value: T
    attempts: int


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy = RetryPolicy(),
    retry_on: tuple[type[BaseException], ...] = (TransientGatewayError,),
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> RetryOutcome[T]:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return RetryOutcome(fn(), attempt)
        except retry_on as exc:
            if attempt == policy.max_attempts:
                raise RetryExhausted(attempt, exc) from exc
            sleep(policy.delay_for(attempt, rng))
    raise AssertionError("unreachable")
