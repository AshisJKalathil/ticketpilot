"""Tool specification and execution context."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from ticketpilot.config import Settings
from ticketpilot.guards.redact import Vault
from ticketpilot.retry import RetryPolicy
from ticketpilot.schemas import InputModel, OutputModel
from ticketpilot.store import Store


class ToolError(Exception):
    """A business-level failure the model should see and adapt to (not retried)."""


class PermissionDenied(ToolError):
    pass


@dataclass
class ToolContext:
    store: Store
    settings: Settings
    run_id: str
    customer_id: str | None
    scopes: frozenset[str]
    tool_use_id: str = ""
    attempt: int = 1
    vault: Vault | None = None
    edited_by_human: bool = False

    @property
    def idempotency_key(self) -> str:
        return f"{self.run_id}:{self.tool_use_id}"

    @property
    def today(self) -> date:
        return self.settings.store_date

    @property
    def allowed_hosts(self) -> list[str]:
        host = urlparse(self.settings.portal_base_url).hostname
        return [host] if host else []

    def to_wire(self) -> dict[str, Any]:
        """The minimum a sandboxed worker needs - no vault, no credentials."""
        return {
            "db_path": str(self.store.path), "run_id": self.run_id, "customer_id": self.customer_id,
            "scopes": sorted(self.scopes), "tool_use_id": self.tool_use_id, "attempt": self.attempt,
            "store_date": self.settings.store_date.isoformat(), "portal_base_url": self.settings.portal_base_url,
            "browser_engine": self.settings.browser_engine, "fault_injection": self.settings.fault_injection,
        }

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> ToolContext:
        settings = Settings(
            db_path=Path(wire["db_path"]), store_date=date.fromisoformat(wire["store_date"]),
            portal_base_url=wire["portal_base_url"], browser_engine=wire["browser_engine"],
            fault_injection=wire.get("fault_injection", {}),
        )
        return cls(store=Store(wire["db_path"]), settings=settings, run_id=wire["run_id"],
                   customer_id=wire["customer_id"], scopes=frozenset(wire["scopes"]),
                   tool_use_id=wire["tool_use_id"], attempt=wire["attempt"])


Handler = Callable[[ToolContext, Any], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[InputModel]
    output_model: type[OutputModel]
    handler: Handler
    scopes: frozenset[str]
    requires_approval: bool = False
    cache_ttl_s: float | None = None
    isolation: Literal["inproc", "process"] = "inproc"
    timeout_s: float = 10.0
    retry: RetryPolicy | None = None
    dependency: str | None = None  # circuit-breaker key for an external system
    untrusted_fields: tuple[str, ...] = ()  # "field", "list[].field"
    ends_run: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)
    schema_override: dict[str, Any] | None = None  # e.g. a JSON schema declared by an MCP server

    def definition(self) -> dict[str, Any]:
        if self.schema_override is not None:
            return {"name": self.name, "description": self.description, "input_schema": self.schema_override}
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        schema.setdefault("properties", {})
        schema["additionalProperties"] = False
        return {"name": self.name, "description": self.description, "input_schema": schema}
