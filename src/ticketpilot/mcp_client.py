"""Mount tools from external MCP servers into the agent's registry.

Each configured server runs as a stdio subprocess with one long-lived
session on a background event loop; the synchronous agent calls into it with
a timeout. Mounted tools are treated as **third-party**:

* only tools on the config's ``allow_tools`` list are mounted;
* each gets the scopes declared in config (least privilege applies to them too);
* arguments are checked against the server-declared JSON schema;
* results are size-capped, redacted, spotlighted as untrusted and scanned for
  injection by the normal registry pipeline.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import yaml
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import ConfigDict

from ticketpilot.schemas import InputModel, OutputModel
from ticketpilot.tools.base import ToolContext, ToolError, ToolSpec

MAX_RESULT_CHARS = 8000
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


class MCPPassthroughInput(InputModel):
    model_config = ConfigDict(extra="allow")


class MCPToolOutput(OutputModel):
    server: str
    tool: str
    content: str


def check_args(schema: dict, args: dict) -> None:
    props = schema.get("properties", {})
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise ToolError(f"missing required arguments: {missing}")
    if schema.get("additionalProperties") is False:
        extra = [k for k in args if k not in props]
        if extra:
            raise ToolError(f"unexpected arguments: {extra}")
    for key, value in args.items():
        expected = _TYPES.get(props.get(key, {}).get("type", ""))
        if expected and not isinstance(value, expected):
            raise ToolError(f"argument {key!r} should be {props[key]['type']}")


class MCPToolSource:
    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None,
                 timeout_s: float = 20.0):
        self.name = name
        self.params = StdioServerParameters(command=command, args=args, env=env)
        self.timeout_s = timeout_s
        self.tools: list[Any] = []
        self._session: ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> MCPToolSource:
        self._thread = threading.Thread(target=self._run, name=f"mcp-{self.name}", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.timeout_s):
            raise TimeoutError(f"MCP server {self.name} did not start within {self.timeout_s}s")
        if self._error:
            raise RuntimeError(f"MCP server {self.name} failed to start: {self._error}") from self._error
        return self

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        except BaseException as exc:  # surfaced to start()
            self._error = exc
            self._ready.set()

    async def _main(self) -> None:
        self._stop = asyncio.Event()
        async with stdio_client(self.params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            self.tools = (await session.list_tools()).tools
            self._session = session
            self._ready.set()
            await self._stop.wait()

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        if not self._session or not self._loop:
            raise ToolError(f"MCP server {self.name} is not connected")
        future = asyncio.run_coroutine_threadsafe(self._session.call_tool(tool, args), self._loop)
        return future.result(timeout=self.timeout_s)

    def close(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=5)

    def specs(self, *, prefix: str, scopes: frozenset[str], allow: list[str]) -> list[ToolSpec]:
        out = []
        for tool in self.tools:
            if tool.name not in allow:
                continue
            schema = dict(tool.input_schema or {"type": "object", "properties": {}})

            def handler(ctx: ToolContext, args: MCPPassthroughInput, _tool=tool.name, _schema=schema) -> dict:
                payload = args.model_dump()
                check_args(_schema, payload)
                result = self.call(_tool, payload)
                if getattr(result, "is_error", False):
                    raise ToolError(f"{self.name}.{_tool} failed: {self._text(result)[:300]}")
                return {"server": self.name, "tool": _tool, "content": self._text(result)[:MAX_RESULT_CHARS]}

            out.append(ToolSpec(
                name=f"{prefix}{tool.name}",
                description=f"[MCP:{self.name}] {tool.description or tool.name}",
                input_model=MCPPassthroughInput, output_model=MCPToolOutput, handler=handler, scopes=scopes,
                timeout_s=self.timeout_s, untrusted_fields=("content",), schema_override=schema,
            ))
        return out

    @staticmethod
    def _text(result: Any) -> str:
        structured = getattr(result, "structured_content", None)
        if structured:
            return json.dumps(structured)
        return "\n".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [])


_sources: dict[str, list[MCPToolSource]] = {}
_lock = threading.Lock()


def mount_from_config(registry, path: Path) -> list[str]:
    """Start (once per process) and mount every server in the config file."""
    key = str(Path(path).resolve())
    with _lock:
        if key not in _sources:
            config = yaml.safe_load(Path(path).read_text()) or {}
            _sources[key] = [
                (MCPToolSource(s["name"], s["command"], s.get("args", []), s.get("env")).start(), s)
                for s in config.get("servers", [])
            ]
    mounted = []
    for source, cfg in _sources[key]:
        for spec in source.specs(prefix=cfg.get("prefix", f"{cfg['name']}_"), scopes=frozenset(cfg.get("scopes", [])),
                                 allow=cfg.get("allow_tools", [])):
            if registry.get(spec.name) is None:
                registry.mount(spec)
                mounted.append(spec.name)
    return mounted
