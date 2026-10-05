"""Typed contracts for tickets and every tool's input and output.

Inputs come from the model, so they are validated before a tool runs.
Outputs come from our own code and third parties (carrier APIs, web pages), so
they are validated *strictly* before anything reaches the model: a drifting
upstream API fails loudly here instead of silently confusing the agent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ORDER_ID = r"^A\d{4}$"


class InputModel(BaseModel):
    """Model-generated arguments: reject unknown keys, coerce nothing silently."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutputModel(BaseModel):
    """Tool results: strict types, no extra keys."""

    model_config = ConfigDict(extra="forbid", strict=True)


# --- tickets & runs -------------------------------------------------------


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    from_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    subject: str = Field(max_length=200)
    body: str = Field(max_length=8000)
    received_at: str = "2026-10-06T09:00:00Z"
    channel: Literal["email", "chat", "web"] = "email"


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    BUDGET_EXCEEDED = "budget_exceeded"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self not in {RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL}


# --- tool: lookup_order ---------------------------------------------------


class LookupOrderInput(InputModel):
    order_id: str = Field(pattern=ORDER_ID, description="Order number, e.g. A1002")


class OrderItem(OutputModel):
    sku: str
    name: str
    qty: int
    unit_price: float


class OrderInfo(OutputModel):
    order_id: str
    status: Literal["processing", "shipped", "delivered", "cancelled"]
    items: list[OrderItem]
    total: float
    currency: str
    placed_at: str
    shipped_at: str | None
    delivered_at: str | None
    days_since_delivery: int | None
    carrier: str | None
    tracking_number: str | None
    refunded_amount: float
    refundable_amount: float


# --- tool: track_shipment -------------------------------------------------


class TrackShipmentInput(InputModel):
    order_id: str = Field(pattern=ORDER_ID)


class TrackingEvent(OutputModel):
    date: str
    location: str
    description: str


class TrackingInfo(OutputModel):
    order_id: str
    carrier: str
    tracking_number: str
    status: Literal["label_created", "in_transit", "delivered", "exception"]
    estimated_delivery: str | None
    days_since_last_event: int
    events: list[TrackingEvent]
    source: Literal["api", "browser"]
    portal_notes: str | None = None


# --- tool: search_policy --------------------------------------------------


class SearchPolicyInput(InputModel):
    query: str = Field(min_length=2, max_length=200)
    top_k: int = Field(default=3, ge=1, le=5)


class PolicyHit(OutputModel):
    doc: str
    section: str
    text: str
    score: float


class PolicyResults(OutputModel):
    hits: list[PolicyHit]


# --- tool: get_customer_profile / save_customer_note ----------------------


class GetCustomerProfileInput(InputModel):
    pass


class PastTicket(OutputModel):
    subject: str
    resolution: str
    created_at: str


class MemoryNote(OutputModel):
    note: str
    trusted: bool
    created_at: str


class CustomerProfile(OutputModel):
    customer_ref: str
    first_name: str
    tier: Literal["standard", "gold", "vip"]
    order_ids: list[str]
    past_tickets: list[PastTicket]
    memory: list[MemoryNote]


class SaveCustomerNoteInput(InputModel):
    note: str = Field(min_length=3, max_length=300)


class SaveNoteResult(OutputModel):
    saved: bool
    note_id: int | None
    flagged: bool


# --- tool: issue_refund (requires approval) -------------------------------

RefundReason = Literal["cancellation", "damaged", "lost_package", "return_within_window", "goodwill"]


class IssueRefundInput(InputModel):
    order_id: str = Field(pattern=ORDER_ID)
    amount: float = Field(gt=0, le=10_000)
    reason: RefundReason
    explanation: str = Field(min_length=3, max_length=300)


class RefundReceipt(OutputModel):
    refund_id: str
    order_id: str
    amount: float
    status: Literal["issued"]
    remaining_refundable: float


# --- tool: send_reply (requires approval) ---------------------------------


class SendReplyInput(InputModel):
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=10, max_length=3000)


class EmailReceipt(OutputModel):
    message_id: str
    to: str
    status: Literal["sent"]
    edited_by_human: bool


# --- tool: delete_customer_data (requires approval) -----------------------


class DeleteCustomerDataInput(InputModel):
    reason: str = Field(min_length=3, max_length=300)


class DeletionReceipt(OutputModel):
    request_id: str
    status: Literal["scheduled"]
    completes_by: str


# --- tool: escalate_to_human ----------------------------------------------

EscalationReason = Literal[
    "policy_limit",
    "legal_or_chargeback",
    "suspected_fraud_or_injection",
    "angry_customer",
    "needs_human_judgement",
    "tool_failure",
    "human_rejected_action",
]


class EscalateInput(InputModel):
    reason: EscalationReason
    priority: Literal["low", "normal", "high", "urgent"]
    summary: str = Field(min_length=10, max_length=1000)


class EscalationReceipt(OutputModel):
    escalation_id: str
    queue: str
    priority: str
