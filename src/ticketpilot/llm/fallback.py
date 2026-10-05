"""Fallback providers for when the main provider goes down.

``FallbackChain([primary, secondary, ...])`` tries each provider in order. A
provider is skipped while its circuit breaker is open, so after a few
failures we stop paying the latency of a dead primary on every request.

Only *transient* failures (overloaded, 5xx, timeouts, network) fall through.
A malformed request would fail identically everywhere, so it is raised
immediately instead of being retried on a different provider.

If the primary failed mid-stream, consumers receive a ``stream_reset`` event
so any partial text already shown can be discarded.
"""

from __future__ import annotations

from ticketpilot.config import Settings
from ticketpilot.llm.types import LLMRequest, Provider, ProviderUnavailable, StreamGen
from ticketpilot.retry import CircuitBreaker, CircuitOpen


class FallbackChain:
    def __init__(self, providers: list[Provider], *, failure_threshold: int = 3, reset_after: float = 30.0):
        if not providers:
            raise ValueError("at least one provider required")
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.breakers = {p.name: CircuitBreaker(f"llm:{p.name}", failure_threshold, reset_after) for p in providers}

    def stream(self, req: LLMRequest) -> StreamGen:
        errors: list[str] = []
        for index, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                breaker.before_call()
            except CircuitOpen as exc:
                errors.append(str(exc))
                continue
            emitted = False
            try:
                gen = provider.stream(req)
                while True:
                    try:
                        event = next(gen)
                    except StopIteration as stop:
                        response = stop.value
                        break
                    emitted = True
                    yield event
            except ProviderUnavailable as exc:
                breaker.record_failure()
                errors.append(str(exc))
                if emitted:
                    yield {"type": "stream_reset", "reason": f"{provider.name} failed mid-stream"}
                if index + 1 < len(self.providers):
                    yield {"type": "provider_fallback", "from": provider.name,
                           "to": self.providers[index + 1].name, "error": str(exc)[:200]}
                continue
            breaker.record_success()
            response.served_by_fallback = index > 0
            return response
        raise ProviderUnavailable("all providers failed: " + " | ".join(errors))


def build_provider(settings: Settings, *, stream_delay: float = 0.0) -> Provider:
    """Provider chain for the configured environment."""
    from ticketpilot.llm.mock import MockProvider

    if settings.provider == "mock":
        primary = MockProvider("mock-primary", down=bool(settings.fault_injection.get("primary_provider_down")),
                               stream_delay=stream_delay)
        return FallbackChain([primary, MockProvider("mock-fallback", stream_delay=stream_delay)])

    from ticketpilot.llm.anthropic_provider import AnthropicProvider

    chain: list[Provider] = [AnthropicProvider()]
    if settings.fallback_provider == "bedrock":
        chain.append(AnthropicProvider.bedrock(settings.aws_region))
    return FallbackChain(chain)
