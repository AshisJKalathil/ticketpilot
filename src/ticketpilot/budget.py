"""Cap every run with token, time, step and cost budgets.

Budgets are enforced at three points:

1. **Pre-flight** - before a model call, the estimated input cost must fit in
   what is left, and ``max_tokens`` is shrunk so the *worst-case* output still
   fits. We never start a call we cannot afford.
2. **Post-hoc** - after the call, actual usage is charged; if a cap is now
   exceeded the run stops *before* executing any tool the model asked for, so
   an over-budget run never causes side effects.
3. **Wall clock** - active time (excluding time spent waiting for a human
   approval) is capped, and retries respect the remaining time as a deadline.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from ticketpilot.llm.pricing import Usage, cost_usd, price_for

MIN_USEFUL_OUTPUT_TOKENS = 512


class BudgetExceeded(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind} budget exceeded: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass
class Budget:
    max_cost_usd: float
    max_seconds: float
    max_steps: int
    max_total_tokens: int
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    steps: int = 0
    active_seconds: float = 0.0
    _segment_start: float | None = None

    # --- accounting -----------------------------------------------------
    def start_clock(self) -> None:
        self._segment_start = time.monotonic()

    def stop_clock(self) -> None:
        if self._segment_start is not None:
            self.active_seconds += time.monotonic() - self._segment_start
            self._segment_start = None

    @property
    def elapsed(self) -> float:
        running = time.monotonic() - self._segment_start if self._segment_start is not None else 0.0
        return self.active_seconds + running

    def deadline(self) -> float:
        """Monotonic timestamp after which retries should stop."""
        return time.monotonic() + max(0.0, self.max_seconds - self.elapsed)

    def charge(self, model: str, usage: Usage) -> float:
        cost = cost_usd(model, usage)
        self.cost_usd += cost
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_input_tokens
        self.cache_write_tokens += usage.cache_creation_input_tokens
        return cost

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    # --- enforcement ----------------------------------------------------
    def check(self) -> None:
        if self.cost_usd > self.max_cost_usd:
            raise BudgetExceeded("cost", f"${self.cost_usd:.4f} > ${self.max_cost_usd:.4f}")
        if self.total_tokens > self.max_total_tokens:
            raise BudgetExceeded("tokens", f"{self.total_tokens} > {self.max_total_tokens}")
        if self.elapsed > self.max_seconds:
            raise BudgetExceeded("time", f"{self.elapsed:.1f}s > {self.max_seconds:.0f}s")
        if self.steps > self.max_steps:
            raise BudgetExceeded("steps", f"{self.steps} > {self.max_steps}")

    def preflight(self, model: str, est_input_tokens: int, desired_max_tokens: int) -> int:
        """Return an affordable ``max_tokens`` for the next call, or raise."""
        self.check()
        if self.steps + 1 > self.max_steps:
            raise BudgetExceeded("steps", f"next step {self.steps + 1} > {self.max_steps}")
        p = price_for(model)
        remaining = self.max_cost_usd - self.cost_usd - est_input_tokens * p.input / 1_000_000
        if remaining <= 0:
            raise BudgetExceeded("cost", f"next call input alone (~{est_input_tokens} tok) exceeds the remaining budget")
        affordable = int(remaining * 1_000_000 / p.output)
        token_room = self.max_total_tokens - self.total_tokens - est_input_tokens
        max_tokens = min(desired_max_tokens, affordable, token_room)
        if max_tokens < MIN_USEFUL_OUTPUT_TOKENS:
            raise BudgetExceeded("cost", f"only {max_tokens} output tokens affordable")
        return max_tokens

    # --- persistence ----------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_segment_start")
        return d

    @classmethod
    def from_dict(cls, data: dict) -> Budget:
        return cls(**{k: v for k, v in data.items() if not k.startswith("_")})
