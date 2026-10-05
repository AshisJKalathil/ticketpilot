"""Make agent steps resumable so crashes don't restart everything.

The full run state is checkpointed to SQLite after every model response and
after *every individual tool result*. On resume:

* model calls that already returned are never re-paid - their responses are
  in the message history;
* tools that already ran are not re-run - their results sit in
  ``pending.results`` until the whole turn can be appended;
* side-effecting tools carry an idempotency key (``run_id:tool_use_id``), so
  even a crash *between* a refund and its checkpoint cannot refund twice.

The rendered system prompt is stored with the run, so a resumed run uses the
exact same bytes even if the prompt file was edited in between (append-only
history keeps prompt caching valid).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from ticketpilot.schemas import RunStatus
from ticketpilot.store import Store, utcnow


@dataclass
class RunState:
    run_id: str
    ticket_id: str
    customer_id: str | None
    customer_tier: str | None
    customer_first_name: str | None
    ticket_redacted: dict[str, Any]
    signals: dict[str, Any]
    scopes: list[str]
    tainted: bool
    route: dict[str, Any]
    model: str
    effort: str | None
    prompt_ref: str
    system: str
    messages: list[dict[str, Any]]
    budget: dict[str, Any]
    vault: dict[str, Any]
    status: str = RunStatus.RUNNING.value
    arm: str | None = None
    pending: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    guard_flags: dict[str, Any] = field(default_factory=lambda: {"injection": [], "pii": []})
    final_reply: str | None = None
    summary: str | None = None
    served_by_fallback: bool = False
    invalid_streak: int = 0
    answer_cache: dict[str, Any] | None = None
    ledger_charged: float = 0.0
    crashed_once: bool = False
    created_at: str = field(default_factory=utcnow)

    def tool_call(self, tool_use_id: str) -> dict[str, Any] | None:
        return next((c for c in self.tool_calls if c["id"] == tool_use_id), None)


def save(store: Store, state: RunState) -> None:
    b = state.budget
    store.execute(
        "INSERT INTO runs(id,ticket_id,status,route,model,arm,prompt_version,cost_usd,input_tokens,output_tokens,"
        "steps,active_seconds,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET status=excluded.status, model=excluded.model, cost_usd=excluded.cost_usd, "
        "input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens, steps=excluded.steps, "
        "active_seconds=excluded.active_seconds, state_json=excluded.state_json, updated_at=excluded.updated_at",
        (state.run_id, state.ticket_id, state.status, state.route.get("route"), state.model, state.arm,
         state.prompt_ref, b.get("cost_usd", 0.0), b.get("input_tokens", 0), b.get("output_tokens", 0),
         b.get("steps", 0), b.get("active_seconds", 0.0), json.dumps(asdict(state)), state.created_at, utcnow()),
    )


def load(store: Store, run_id: str) -> RunState | None:
    row = store.one("SELECT state_json FROM runs WHERE id=?", (run_id,))
    return RunState(**json.loads(row["state_json"])) if row else None


def interrupted_runs(store: Store) -> list[str]:
    """Runs that were mid-flight when the process died (status still 'running')."""
    return [r["id"] for r in store.query("SELECT id FROM runs WHERE status='running' ORDER BY updated_at")]
