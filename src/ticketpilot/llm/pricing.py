"""Model prices (USD per million tokens) and cost accounting.

First-party Claude API list prices. Bedrock / Vertex are partner-priced; map
their model IDs back to the first-party name for an estimate and reconcile
against the real invoice.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float  # 5-minute TTL write = 1.25x input
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": Price(2.00, 10.00, 2.50, 0.20),
    "claude-haiku-4-5": Price(1.00, 5.00, 1.25, 0.10),
    "claude-opus-5": Price(5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-8": Price(5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": Price(2.00, 10.00, 2.50, 0.20),
}


def price_for(model: str) -> Price:
    base = model.removeprefix("anthropic.").removeprefix("us.anthropic.")
    if base in PRICES:
        return PRICES[base]
    # Unknown model: assume the most expensive tier so budgets fail safe.
    return max(PRICES.values(), key=lambda p: p.output)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
        )

    @property
    def total(self) -> int:
        return (self.input_tokens + self.output_tokens + self.cache_creation_input_tokens
                + self.cache_read_input_tokens)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def cost_usd(model: str, usage: Usage) -> float:
    p = price_for(model)
    return (
        usage.input_tokens * p.input
        + usage.output_tokens * p.output
        + usage.cache_creation_input_tokens * p.cache_write
        + usage.cache_read_input_tokens * p.cache_read
    ) / 1_000_000
