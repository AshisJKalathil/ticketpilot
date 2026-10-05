"""Claude via the official Anthropic SDK (first-party API or Amazon Bedrock).

Production details handled here:

* **Streaming** for every call (`messages.stream`), so the UI shows text and the
  draft reply as they are generated, and long calls never hit HTTP timeouts.
* **Prompt caching**: the tool list and system prompt are a stable prefix
  (deterministic tool order, no timestamps), cached with ``cache_control``;
  top-level automatic caching also covers the growing conversation.
* **Eager tool-input streaming** on the first-party API so the reply body
  streams token by token; inputs are then validated client-side (pydantic) by
  the tool registry before anything runs.
* **Server-side refusal fallback** (``fallbacks="default"``) on Claude Opus 5.5.
* **Error mapping**: transient errors (429/5xx/529/network) raise
  :class:`ProviderUnavailable` so the fallback chain can take over; request
  errors raise :class:`ProviderFatal` and are never retried elsewhere.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from ticketpilot.llm.pricing import Usage
from ticketpilot.llm.types import (
    LLMRequest,
    LLMResponse,
    MalformedToolInput,
    ProviderFatal,
    ProviderUnavailable,
    StreamGen,
)

TRANSIENT_ERRORS = (
    anthropic.APIConnectionError,  # includes APITimeoutError
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.OverloadedError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
)
FATAL_ERRORS = (
    anthropic.BadRequestError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.RequestTooLargeError,
    anthropic.UnprocessableEntityError,
)

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _supports_effort(model: str) -> bool:
    # Haiku 4.5 rejects output_config.effort.
    return "haiku" not in model


def _supports_server_fallback(model: str) -> bool:
    return model.startswith("claude-opus-5")


class AnthropicProvider:
    def __init__(
        self,
        client: Any | None = None,
        *,
        name: str = "anthropic",
        model_map: dict[str, str] | None = None,
        first_party: bool = True,
        max_retries: int = 2,
    ):
        self.client = client or anthropic.Anthropic(max_retries=max_retries)
        self.name = name
        self.model_map = model_map or {}
        self.first_party = first_party

    @classmethod
    def bedrock(cls, region: str) -> AnthropicProvider:
        client = anthropic.AnthropicBedrockMantle(aws_region=region)
        mapping = {m: f"anthropic.{m}" for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5")}
        return cls(client, name="bedrock", model_map=mapping, first_party=False)

    def _params(self, req: LLMRequest) -> dict[str, Any]:
        model = self.model_map.get(req.model, req.model)
        tools = [dict(t) for t in req.tools]
        if self.first_party:
            for t in tools:
                t["eager_input_streaming"] = True
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            # Stable prefix (tools -> system) cached explicitly; works on every platform.
            "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
            "messages": req.messages,
            "tools": tools,
        }
        if self.first_party:
            params["cache_control"] = {"type": "ephemeral"}  # auto-cache the growing conversation
        if req.effort and _supports_effort(req.model):
            params["output_config"] = {"effort": req.effort}
        betas = []
        if self.first_party and _supports_server_fallback(req.model):
            params["fallbacks"] = "default"
            betas.append(SERVER_FALLBACK_BETA)
        if betas:
            params["betas"] = betas
        return params

    def stream(self, req: LLMRequest) -> StreamGen:
        params = self._params(req)
        started = time.perf_counter()
        current_tool: str | None = None
        try:
            with self.client.beta.messages.stream(**params) as stream:
                for event in stream:
                    if event.type == "text":
                        yield {"type": "text_delta", "text": event.text}
                    elif event.type == "content_block_start":
                        block = event.content_block
                        current_tool = block.name if block.type == "tool_use" else None
                        if current_tool:
                            yield {"type": "tool_input_start", "tool": current_tool}
                    elif event.type == "input_json" and current_tool:
                        yield {"type": "tool_input_delta", "tool": current_tool, "snapshot": event.snapshot}
                message = stream.get_final_message()
        except ValueError as exc:
            # Eager input streaming: the SDK could not parse the tool-input JSON at all.
            raise MalformedToolInput(str(exc)) from exc
        except TRANSIENT_ERRORS as exc:
            raise ProviderUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        except FATAL_ERRORS as exc:
            raise ProviderFatal(f"{self.name}: {type(exc).__name__}: {exc}") from exc

        u = message.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        )
        return LLMResponse(
            model=req.model,
            # Keep every block (thinking, fallback, tool_use...) exactly as returned:
            # the history is append-only and is echoed back unchanged next turn.
            content=[b.model_dump(mode="json", exclude_none=True) for b in message.content],
            stop_reason=message.stop_reason or "end_turn",
            usage=usage,
            provider=self.name,
            latency_s=time.perf_counter() - started,
            request_id=getattr(message, "_request_id", None),
        )
