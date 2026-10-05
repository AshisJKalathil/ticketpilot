"""Provider-neutral request/response types for the agent loop.

Messages and content blocks are kept in the Anthropic wire format as plain
JSON-serialisable dicts, so a run can be checkpointed to disk and resumed
byte-for-byte (append-only history keeps prompt caching and preserved thinking
valid across resumes).
"""

from __future__ import annotations

import hashlib
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, Protocol

from ticketpilot.llm.pricing import Usage
from ticketpilot.store import dumps

# A provider streams events (dicts) and *returns* the final response:
#     response = yield from provider.stream(request)
StreamGen = Generator[dict[str, Any], None, "LLMResponse"]


class ProviderUnavailable(Exception):
    """Transient provider failure (overloaded, 5xx, timeout, network). Try a fallback."""


class ProviderFatal(Exception):
    """Non-retryable failure (bad request, auth). Do not fall back - fix the request."""


class MalformedToolInput(Exception):
    """The model emitted tool-input JSON the SDK could not parse; re-issue the turn."""


@dataclass
class LLMRequest:
    model: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_tokens: int
    effort: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # not sent to the provider

    def payload(self) -> dict[str, Any]:
        return {"model": self.model, "system": self.system, "messages": self.messages, "tools": self.tools,
                "effort": self.effort}

    def fingerprint(self) -> str:
        return hashlib.sha256(dumps(self.payload()).encode()).hexdigest()

    def estimated_input_tokens(self) -> int:
        """~4 characters per token. Good enough for pre-flight budget checks."""
        return len(dumps(self.payload())) // 4


@dataclass
class LLMResponse:
    model: str
    content: list[dict[str, Any]]
    stop_reason: str
    usage: Usage
    provider: str
    latency_s: float = 0.0
    request_id: str | None = None
    served_by_fallback: bool = False

    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class Provider(Protocol):
    name: str

    def stream(self, request: LLMRequest) -> StreamGen: ...
