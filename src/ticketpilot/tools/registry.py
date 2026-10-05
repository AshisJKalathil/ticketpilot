"""Tool exposure and the execution pipeline.

``execute`` is the only way a tool runs. In order:

1. scope check (least privilege) - unknown or ungranted tools are refused;
2. input validation against the tool's pydantic model;
3. tool-result cache (read-only tools only);
4. circuit breaker for the tool's external dependency;
5. retry with exponential backoff + jitter, bounded by the run's deadline;
6. sandboxed execution (in-process or a scrubbed subprocess);
7. **strict output validation** - a drifting upstream fails here, loudly;
8. PII/secret redaction of the output (the model only sees placeholders);
9. spotlighting + injection scanning of third-party text fields.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ticketpilot.cache import ToolCache
from ticketpilot.guards.injection import scan, wrap_untrusted
from ticketpilot.guards.redact import Vault, redact_obj
from ticketpilot.retry import (
    CircuitOpen,
    RetryExhausted,
    RetryPolicy,
    TransientError,
    breaker,
    call_with_retry,
)
from ticketpilot.sandbox import Sandbox
from ticketpilot.tools.base import PermissionDenied, ToolContext, ToolError, ToolSpec
from ticketpilot.tools.catalog import CATALOG

NO_RETRY = RetryPolicy(max_attempts=1)


@dataclass
class ToolOutcome:
    name: str
    ok: bool
    output: dict[str, Any] | None = None  # redacted, un-wrapped (logs, UI, evals)
    error: str | None = None
    error_kind: str | None = None  # invalid_input | permission | business | schema | transient | circuit_open
    attempts: int = 1
    cached: bool = False
    duration_ms: float = 0.0
    injection: list[dict] = field(default_factory=list)
    model_content: str = ""  # what goes back to the model in the tool_result block

    def as_log(self) -> dict[str, Any]:
        return {"ok": self.ok, "output": self.output, "error": self.error, "error_kind": self.error_kind,
                "attempts": self.attempts, "cached": self.cached, "duration_ms": round(self.duration_ms, 1),
                "injection": self.injection}


def _wrap_path(obj: Any, path: list[str], source: str, reports: list[dict]) -> Any:
    """Wrap untrusted strings at ``path`` (supports ``list[]`` hops)."""
    if not path:
        if isinstance(obj, str) and obj:
            report = scan(obj, source)
            if report.flagged:
                reports.append(report.as_dict())
            return wrap_untrusted(obj, source)
        return obj
    head, rest = path[0], path[1:]
    if head.endswith("[]"):
        key = head[:-2]
        items = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(items, list):
            obj = dict(obj)
            obj[key] = [item if isinstance(item, dict) and item.get("trusted") is True
                        else _wrap_path(item, rest, source, reports) for item in items]
        return obj
    if isinstance(obj, dict) and head in obj:
        obj = dict(obj)
        obj[head] = _wrap_path(obj[head], rest, source, reports)
    return obj


class ToolRegistry:
    def __init__(self, specs: dict[str, ToolSpec] | None = None, sandbox: Sandbox | None = None):
        self.specs: dict[str, ToolSpec] = dict(specs or CATALOG)
        self.sandbox = sandbox or Sandbox()

    def mount(self, spec: ToolSpec) -> None:
        if spec.name in self.specs:
            raise ValueError(f"tool name collision: {spec.name}")
        self.specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self.specs.get(name)

    def granted(self, scopes: frozenset[str] | set[str]) -> list[ToolSpec]:
        return [s for s in sorted(self.specs.values(), key=lambda s: s.name) if s.scopes <= set(scopes)]

    def definitions(self, scopes: frozenset[str] | set[str]) -> list[dict[str, Any]]:
        """Deterministic order -> stable prompt-cache prefix."""
        return [s.definition() for s in self.granted(scopes)]

    def validate_input(self, spec: ToolSpec, args: Any) -> dict[str, Any]:
        return spec.input_model.model_validate(args).model_dump(mode="json")

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext, *, vault: Vault,
                deadline: float | None = None, on_retry=None) -> ToolOutcome:
        started = time.perf_counter()
        spec = self.specs.get(name)

        def fail(kind: str, message: str, attempts: int = 1) -> ToolOutcome:
            return ToolOutcome(name, False, error=message, error_kind=kind, attempts=attempts,
                               duration_ms=(time.perf_counter() - started) * 1000,
                               model_content=json.dumps({"error": message}))

        if spec is None or not spec.scopes <= ctx.scopes:
            return fail("permission", f"Tool '{name}' is not available for this ticket.")
        try:
            clean_args = self.validate_input(spec, args)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
            return fail("invalid_input", f"Invalid input for {name}: {problems or 'expected an object'}")

        cache = ToolCache(ctx.store) if spec.cache_ttl_s else None
        cache_key = ToolCache.key(name, clean_args, ctx.customer_id or "-") if cache else None
        raw = cache.get(cache_key) if cache else None
        attempts, cached = 0, raw is not None

        if raw is None:
            cb = breaker(f"tool:{spec.dependency}") if spec.dependency else None

            def attempt() -> dict[str, Any]:
                nonlocal attempts
                attempts += 1
                ctx.attempt = attempts
                if cb:
                    cb.before_call()
                try:
                    result = self.sandbox.run(spec, clean_args, ctx)
                except (TransientError, TimeoutError, ConnectionError):
                    if cb:
                        cb.record_failure()
                    raise
                if cb:
                    cb.record_success()
                return result

            try:
                raw, attempts = call_with_retry(attempt, spec.retry or NO_RETRY, deadline=deadline, on_retry=on_retry)
            except CircuitOpen as exc:
                return fail("circuit_open", f"{exc}. Try another approach or escalate.", attempts)
            except RetryExhausted as exc:
                return fail("transient", f"{name} is temporarily unavailable ({exc.last}); "
                                         f"gave up after {exc.attempts} attempts.", exc.attempts)
            except PermissionDenied as exc:
                return fail("permission", str(exc), attempts)
            except ToolError as exc:
                return fail("business", str(exc), attempts)

        try:
            validated = spec.output_model.model_validate(raw).model_dump(mode="json")
        except ValidationError as exc:
            return fail("schema", f"{name} returned data that failed schema validation "
                                  f"({exc.error_count()} errors); the result was discarded.", attempts)
        if cache and not cached:
            cache.put(cache_key, validated, spec.cache_ttl_s)

        redacted = redact_obj(validated, vault)
        reports: list[dict] = []
        for_model = redacted
        for path in spec.untrusted_fields:
            for_model = _wrap_path(for_model, path.split("."), f"{name}.{path}", reports)
        return ToolOutcome(name, True, output=redacted, attempts=max(attempts, 1), cached=cached,
                           duration_ms=(time.perf_counter() - started) * 1000, injection=reports,
                           model_content=json.dumps(for_model))
