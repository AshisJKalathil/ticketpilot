"""Retry failed calls with exponential backoff and jitter - never blind loops.

* Only errors that are *known* to be transient are retried; a 400-style
  validation error fails immediately (retrying it just burns money).
* Delays grow exponentially with full jitter so many workers recovering at
  once do not stampede the dependency.
* Attempts are capped, and an overall deadline (from the run's time budget)
  stops retries that could never finish in time.
* A per-dependency circuit breaker stops calling a dependency that is clearly
  down, and lets one probe through after a cool-off.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


class TransientError(Exception):
    """A failure worth retrying (timeouts, 429/5xx, connection resets)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RetryExhausted(Exception):
    def __init__(self, attempts: int, last: BaseException):
        super().__init__(f"gave up after {attempts} attempts: {last}")
        self.attempts = attempts
        self.last = last


class CircuitOpen(Exception):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.2
    max_delay: float = 4.0
    multiplier: float = 2.0
    retry_on: tuple[type[BaseException], ...] = (TransientError, TimeoutError, ConnectionError)

    def backoff(self, attempt: int) -> float:
        """Full-jitter backoff: uniform(0, min(cap, base * mult^(attempt-1)))."""
        return random.uniform(0, min(self.max_delay, self.base_delay * self.multiplier ** (attempt - 1)))


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy = RetryPolicy(),
    *,
    deadline: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> tuple[T, int]:
    """Run ``fn`` with retries. Returns ``(result, attempts)``."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(), attempt
        except policy.retry_on as exc:
            delay = policy.backoff(attempt)
            hint = getattr(exc, "retry_after", None)
            if hint:
                delay = max(delay, float(hint))
            out_of_time = deadline is not None and time.monotonic() + delay > deadline
            if attempt >= policy.max_attempts or out_of_time:
                raise RetryExhausted(attempt, exc) from exc
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)


class CircuitBreaker:
    """Closed -> open after N consecutive failures -> half-open after cool-off."""

    def __init__(self, name: str, failure_threshold: int = 5, reset_after: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self._opened_at >= self.reset_after else "open"

    def before_call(self) -> None:
        with self._lock:
            if self.state == "open":
                raise CircuitOpen(f"circuit '{self.name}' is open - dependency marked down")

    def record_success(self) -> None:
        with self._lock:
            self._failures, self._opened_at = 0, None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold or self.state == "half_open":
                self._opened_at = self._clock()


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def breaker(name: str, **kwargs) -> CircuitBreaker:
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(name, **kwargs)
        return _breakers[name]


def reset_breakers() -> None:
    with _breakers_lock:
        _breakers.clear()
