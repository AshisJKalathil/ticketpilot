"""Drive a run to a terminal state without a UI - used by evals, tests and the CLI.

Approvals are decided by a policy (default: approve everything) standing in
for the human reviewer, and every decision is still recorded as an approval,
so the trajectory scorers can verify gating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ticketpilot.agent import Agent
from ticketpilot.checkpoint import load
from ticketpilot.config import Settings
from ticketpilot.schemas import RunStatus, Ticket
from ticketpilot.store import Store


@dataclass
class RunOutcome:
    run_id: str
    status: str
    route: str | None
    model: str
    steps: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    tool_calls: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    final_reply: str | None
    summary: str | None
    refunds: list[dict[str, Any]]
    deletions: list[dict[str, Any]]
    escalations: list[dict[str, Any]]
    guard_flags: dict[str, Any]
    served_by_fallback: bool
    llm_inputs: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def run_ticket_to_completion(
    ticket: Ticket,
    *,
    settings: Settings,
    store: Store,
    approvals: dict[str, str] | None = None,
    approver: str = "eval-policy",
    max_rounds: int = 10,
    agent: Agent | None = None,
) -> RunOutcome:
    request_log: list[str] = []
    agent = agent or Agent(settings.with_overrides(experiments_enabled=False), store, request_log=request_log)
    events = list(agent.start(ticket))
    run_id = events[0]["run_id"]
    for _ in range(max_rounds):
        state = load(store, run_id)
        if state.status != RunStatus.AWAITING_APPROVAL:
            break
        for apr in agent.approvals.for_run(run_id):
            if apr["status"] != "pending":
                continue
            decision = (approvals or {}).get(apr["tool"], "approve")
            events.extend(agent.decide(apr["id"], approved=decision == "approve", by=approver,
                                       note=None if decision == "approve" else "Rejected by review policy."))
            break  # re-read state: deciding may have resumed and finished the run
    return collect_outcome(store, run_id, request_log=request_log, events=events)


def collect_outcome(store: Store, run_id: str, *, request_log: list[str] | None = None,
                    events: list[dict] | None = None) -> RunOutcome:
    state = load(store, run_id)
    b = state.budget
    return RunOutcome(
        run_id=run_id, status=state.status, route=state.route.get("route"), model=state.model,
        steps=b.get("steps", 0), cost_usd=b.get("cost_usd", 0.0), input_tokens=b.get("input_tokens", 0),
        output_tokens=b.get("output_tokens", 0), tool_calls=state.tool_calls,
        approvals=store.query("SELECT tool, status, decided_by FROM approvals WHERE run_id=?", (run_id,)),
        final_reply=state.final_reply, summary=state.summary,
        refunds=store.query("SELECT order_id, amount, reason FROM refunds"),
        deletions=store.query("SELECT customer_id, status FROM deletion_requests"),
        escalations=store.query("SELECT reason, priority, summary FROM escalations WHERE run_id=?", (run_id,)),
        guard_flags=state.guard_flags, served_by_fallback=state.served_by_fallback,
        llm_inputs=request_log or [], events=events or [],
    )
