"""TicketPilot tools as an MCP server.

Lets any MCP client - Claude Desktop, Claude Code, another agent - use the
store's read-only support tools with the same guarantees the agent gets:
scope checks, schema validation, sandboxing and PII redaction on output.

The server is **read-only by design** (least privilege). Refunds, emails and
deletions stay behind the agent's human-approval queue, so no MCP client can
move money or send email.

    python -m ticketpilot.mcp_server            # stdio transport

Claude Desktop / Claude Code config:

    {"mcpServers": {"ticketpilot": {"command": "/path/to/.venv/bin/python",
                                    "args": ["-m", "ticketpilot.mcp_server"]}}}
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations

from ticketpilot import __version__
from ticketpilot.config import Settings
from ticketpilot.data.seed import seed
from ticketpilot.guards.redact import Vault
from ticketpilot.store import Store
from ticketpilot.tools.base import ToolContext
from ticketpilot.tools.kb import KB_DIR
from ticketpilot.tools.registry import ToolRegistry

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
READ_SCOPES = frozenset({"orders:read", "shipping:read", "kb:read", "customer:read"})


def build_server(settings: Settings, store: Store) -> MCPServer:
    registry = ToolRegistry()
    server = MCPServer(
        name="ticketpilot", version=__version__,
        instructions="Read-only tools for Acme Trail Co. customer support: policies, orders and shipment "
                     "tracking. Personal data in results is replaced with placeholders.",
    )

    def run(tool: str, args: dict[str, Any], customer_email: str | None = None) -> dict[str, Any]:
        customer_id = None
        vault = Vault()
        if customer_email:
            row = store.one("SELECT id FROM customers WHERE lower(email)=lower(?)", (customer_email,))
            if not row:
                raise ToolError("No customer with that email address.")
            customer_id = row["id"]
            vault.token_for("EMAIL", customer_email)
        ctx = ToolContext(store=store, settings=settings, run_id=f"mcp_{uuid.uuid4().hex[:10]}",
                          customer_id=customer_id, scopes=READ_SCOPES, tool_use_id="mcp", vault=vault)
        outcome = registry.execute(tool, args, ctx, vault=vault)
        if not outcome.ok:
            raise ToolError(outcome.error)  # anticipated failure: message reaches the client, no traceback
        return outcome.output

    def search_policy(query: str, top_k: int = 3) -> dict[str, Any]:
        """Search store policies (refunds, shipping, privacy, tone, FAQ). Returns the best matching sections."""
        return run("search_policy", {"query": query, "top_k": top_k})

    def lookup_order(customer_email: str, order_id: str) -> dict[str, Any]:
        """Look up an order belonging to the customer with this email (e.g. order_id 'A1002')."""
        return run("lookup_order", {"order_id": order_id}, customer_email)

    def track_shipment(customer_email: str, order_id: str) -> dict[str, Any]:
        """Live carrier tracking for a customer's shipped order (API or browser fallback)."""
        return run("track_shipment", {"order_id": order_id}, customer_email)

    for fn in (search_policy, lookup_order, track_shipment):
        server.add_tool(fn, name=fn.__name__, description=fn.__doc__, annotations=READ_ONLY)

    docs = {p.stem: p for p in sorted(KB_DIR.glob("*.md"))}

    @server.resource("kb://{doc}", name="policy", mime_type="text/markdown",
                     description=f"A policy document. Available: {', '.join(docs)}")
    def read_policy(doc: str) -> str:
        if doc not in docs:  # allowlist - never map a client-supplied name onto the filesystem
            raise ResourceNotFoundError(f"unknown policy document {doc!r}")
        return docs[doc].read_text()

    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="TicketPilot read-only MCP server (stdio)")
    ap.add_argument("--read-only", action="store_true", help="accepted for clarity; the server is always read-only")
    ap.parse_args(argv)
    settings = Settings.from_env()
    store = Store(settings.db_path)
    seed(store)
    build_server(settings, store).run("stdio")


if __name__ == "__main__":
    main()
