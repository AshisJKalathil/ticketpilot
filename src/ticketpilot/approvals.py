"""Require human approval before money, emails or deletes.

When the model asks for a gated tool, the run *pauses* (status
``awaiting_approval``, fully checkpointed) and an approval request is queued
with a human-readable preview and risk notes. Nothing executes until a person
approves; a reviewer can also edit the arguments (e.g. reword the reply) or
reject with a note that is passed back to the model.

Previews show reviewers the real recipient and amounts (rehydrated from the
vault) - the model never sees those raw values.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ticketpilot.guards.redact import Vault
from ticketpilot.store import Store, utcnow
from ticketpilot.tools.catalog import render_outgoing


class ApprovalService:
    def __init__(self, store: Store):
        self.store = store

    def create(self, *, run_id: str, tool_use_id: str, tool: str, args: dict, preview: dict,
               risk_notes: list[str]) -> dict:
        existing = self.store.one("SELECT * FROM approvals WHERE run_id=? AND tool_use_id=?", (run_id, tool_use_id))
        if existing:
            return self._row(existing)
        approval_id = f"apr_{uuid.uuid4().hex[:12]}"
        self.store.execute(
            "INSERT INTO approvals(id,run_id,tool_use_id,tool,args_json,preview_json,risk_notes_json,status,created_at)"
            " VALUES(?,?,?,?,?,?,?,'pending',?)",
            (approval_id, run_id, tool_use_id, tool, json.dumps(args), json.dumps(preview), json.dumps(risk_notes),
             utcnow()),
        )
        return self.get(approval_id)

    def decide(self, approval_id: str, *, approved: bool, by: str, note: str | None = None,
               edited_args: dict | None = None) -> dict:
        row = self.get(approval_id)
        if row is None:
            raise KeyError(approval_id)
        if row["status"] != "pending":
            raise ValueError(f"approval {approval_id} already {row['status']}")
        self.store.execute(
            "UPDATE approvals SET status=?, decided_by=?, decision_note=?, edited_args_json=?, decided_at=? WHERE id=?",
            ("approved" if approved else "rejected", by, note, json.dumps(edited_args) if edited_args else None,
             utcnow(), approval_id),
        )
        return self.get(approval_id)

    def get(self, approval_id: str) -> dict | None:
        row = self.store.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return self._row(row) if row else None

    def for_tool_use(self, run_id: str, tool_use_id: str) -> dict | None:
        row = self.store.one("SELECT * FROM approvals WHERE run_id=? AND tool_use_id=?", (run_id, tool_use_id))
        return self._row(row) if row else None

    def for_run(self, run_id: str) -> list[dict]:
        return [self._row(r) for r in self.store.query(
            "SELECT * FROM approvals WHERE run_id=? ORDER BY created_at", (run_id,))]

    def pending(self, limit: int = 50) -> list[dict]:
        return [self._row(r) for r in self.store.query(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at LIMIT ?", (limit,))]

    @staticmethod
    def _row(row: dict) -> dict:
        out = dict(row)
        for key in ("args_json", "preview_json", "risk_notes_json", "edited_args_json"):
            value = out.pop(key)
            out[key.removesuffix("_json")] = json.loads(value) if value else None
        return out


def build_preview(tool: str, args: dict, *, vault: Vault, store: Store, customer_id: str | None) -> dict[str, Any]:
    customer = store.one("SELECT name, email, tier FROM customers WHERE id=?", (customer_id,)) if customer_id else None
    if tool == "send_reply":
        return {"to": customer["email"] if customer else None, "subject": args.get("subject"),
                "body": render_outgoing(args.get("body", ""), vault)}
    if tool == "issue_refund":
        order = store.one("SELECT total, refunded_amount, status FROM orders WHERE id=?", (args.get("order_id"),))
        return {"customer": customer["name"] if customer else None, **args,
                "order_total": order["total"] if order else None,
                "already_refunded": order["refunded_amount"] if order else None}
    if tool == "delete_customer_data":
        return {"customer": customer["name"] if customer else None, "email": customer["email"] if customer else None,
                "reason": args.get("reason"), "irreversible": True}
    return dict(args)


def risk_notes(tool: str, args: dict, *, tainted: bool, injection_sources: list[str], tier: str | None) -> list[str]:
    notes = []
    if tainted or injection_sources:
        notes.append("Untrusted content in this run contained instructions aimed at the AI "
                     f"({', '.join(sorted(set(injection_sources))) or 'ticket'}). Review carefully.")
    if tool == "issue_refund" and float(args.get("amount", 0)) >= 200:
        notes.append(f"High-value refund (${float(args['amount']):.2f}).")
    if tool == "delete_customer_data":
        notes.append("Irreversible: personal data will be erased within 30 days.")
    if tier == "vip":
        notes.append("VIP customer.")
    return notes
