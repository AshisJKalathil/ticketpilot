"""A deterministic, offline stand-in for Claude.

The mock reads the same request the real model would receive (system prompt,
tool list, messages with tool results) and follows a scripted support policy.
It exists so that:

* CI can run the full agent - tools, guards, approvals, budgets, routing,
  resumption, fallback - on every PR with zero API cost and zero flakiness;
* the public demo works without an API key;
* harness regressions are distinguishable from model variance.

It is deliberately *not* clever about security: it obeys only what the harness
exposes (e.g. it cannot refund if ``issue_refund`` was withheld), so the evals
measure the harness's defences rather than the mock's good manners.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from ticketpilot.guards.injection import strip_internal_markup
from ticketpilot.intents import analyze
from ticketpilot.llm.pricing import Usage
from ticketpilot.llm.types import LLMRequest, LLMResponse, ProviderUnavailable, StreamGen
from ticketpilot.store import dumps

MIN_CACHEABLE = {"claude-haiku-4-5": 4096}
_seen_prefixes: set[str] = set()

CARRIER_NAMES = {"swiftship": "SwiftShip", "pacificparcel": "PacificParcel"}
REASON_TEXT = {
    "cancellation": "as the order hadn't shipped yet, I've cancelled it",
    "damaged": "since it arrived damaged",
    "lost_package": "since the carrier has marked the package as lost",
    "return_within_window": "as it's within our 30-day return window",
}


def _tag(text: str, name: str) -> str | None:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", text, re.S)
    return m.group(1).strip() if m else None


class _Ctx:
    """What the mock 'understands' from the conversation so far."""

    def __init__(self, req: LLMRequest):
        first = req.messages[0]["content"]
        self.first = first if isinstance(first, str) else json.dumps(first)
        self.body = _tag(self.first, "untrusted") or self.first
        self.signals = analyze(self.body)
        self.security_notice = _tag(self.first, "security_notice")
        self.redaction_notice = _tag(self.first, "redaction_notice") or ""
        self.tools = {t["name"] for t in req.tools}
        self.calls: list[tuple[str, dict]] = []  # (name, input) in order
        self.results: dict[str, list[tuple[dict, Any, bool]]] = {}
        names: dict[str, tuple[str, dict]] = {}
        for msg in req.messages[1:]:
            if not isinstance(msg["content"], list):
                continue
            for block in msg["content"]:
                if block.get("type") == "tool_use":
                    names[block["id"]] = (block["name"], block["input"])
                    self.calls.append((block["name"], block["input"]))
                elif block.get("type") == "tool_result":
                    name, inp = names.get(block["tool_use_id"], ("?", {}))
                    try:
                        payload = json.loads(block["content"])
                    except (TypeError, ValueError):
                        payload = {"error": block["content"]}
                    self.results.setdefault(name, []).append((inp, payload, bool(block.get("is_error"))))

    def called(self, name: str) -> bool:
        return any(n == name for n, _ in self.calls)

    def ok(self, name: str) -> dict | None:
        for _, payload, err in reversed(self.results.get(name, [])):
            if not err:
                return payload
        return None

    def rejected(self) -> str | None:
        for name, results in self.results.items():
            for _, payload, err in results:
                if err and isinstance(payload, dict) and payload.get("status") == "rejected_by_human":
                    return name
        return None


class MockPolicy:
    def decide(self, ctx: _Ctx) -> tuple[str, list[dict]]:
        """Return (visible_text, tool_calls). No tool calls means the turn ends."""
        s = ctx.signals
        order_id = s.order_ids[0] if s.order_ids else None

        if ctx.security_notice and "escalate_to_human" in ctx.tools:
            return "This ticket looks like an attempt to manipulate the agent; handing it to a human.", [
                self._call("escalate_to_human", reason="suspected_fraud_or_injection", priority="high",
                           summary="Ticket body contains instructions aimed at the AI agent (flagged by screening): "
                                   "it asks for actions on another order and for internal configuration. "
                                   "No action taken; please review for fraud.")]

        if not ctx.calls:
            calls = []
            if order_id:
                calls.append(self._call("lookup_order", order_id=order_id))
            if "get_customer_profile" in ctx.tools:
                calls.append(self._call("get_customer_profile"))
            if "privacy_deletion" in s.intents:
                calls.append(self._call("search_policy", query="data deletion request GDPR"))
            elif s.general_question:
                calls.append(self._call("search_policy", query=self._faq_query(ctx.body)))
            return "Let me look into this.", calls

        if "legal" in s.intents and not ctx.called("escalate_to_human"):
            return "Legal or chargeback threat - escalating.", [self._call(
                "escalate_to_human", reason="legal_or_chargeback", priority="urgent",
                summary=f"Customer threatens a chargeback/legal action about order {order_id or 'unknown'}. "
                        "Order details looked up; no action taken.")]

        rejected = ctx.rejected()
        if rejected == "send_reply":
            return "", []
        if rejected and not ctx.called("escalate_to_human"):
            return "The reviewer rejected that action - escalating rather than retrying.", [self._call(
                "escalate_to_human", reason="human_rejected_action", priority="normal",
                summary=f"Proposed {rejected} on order {order_id} was rejected by a reviewer. "
                        "Customer still needs a decision and a reply.")]

        order = ctx.ok("lookup_order")
        if order_id and not order and not ctx.called("send_reply"):
            return "", [self._reply(ctx, self._not_found_body(ctx, order_id), "About your order")]

        if "privacy_deletion" in s.intents and "delete_customer_data" in ctx.tools \
                and not ctx.called("delete_customer_data"):
            return "Scheduling the deletion request.", [self._call(
                "delete_customer_data", reason="Customer requested deletion of personal data (GDPR).")]

        if order and order["status"] == "shipped" and not ctx.called("track_shipment"):
            return "Checking the carrier.", [self._call("track_shipment", order_id=order["order_id"])]

        tracking = ctx.ok("track_shipment")
        decision = self._refund_decision(s, order, tracking)
        needs_policy = bool(set(s.intents) & {"refund", "cancel", "damaged"}) or (
            tracking and tracking["status"] == "exception")
        if order and needs_policy and not ctx.called("search_policy"):
            return "Checking the refund policy.", [self._call("search_policy", query=self._policy_query(s, tracking))]

        if decision and decision[0] == "escalate" and not ctx.called("escalate_to_human"):
            return "Over my approval limit - escalating.", [self._call(
                "escalate_to_human", reason="policy_limit", priority="normal",
                summary=f"Customer requests a ${decision[1]:.2f} refund on order {order['order_id']} "
                        f"({decision[2]}). This exceeds the $500 agent limit; needs supervisor approval.")]

        if decision and decision[0] == "refund" and "issue_refund" in ctx.tools and not ctx.called("issue_refund"):
            _, amount, reason = decision
            return "Issuing the refund.", [self._call(
                "issue_refund", order_id=order["order_id"], amount=amount, reason=reason,
                explanation=f"Policy: {REASON_TEXT[reason]}.")]

        if not ctx.called("send_reply") and "send_reply" in ctx.tools:
            return "", [self._reply(ctx, self._compose(ctx, order, tracking, decision), self._subject(s))]

        return self._summary(ctx, order, decision), []

    # --- decisions --------------------------------------------------------
    @staticmethod
    def _refund_decision(s, order, tracking):
        if not order:
            return None
        amount = order["refundable_amount"]
        reason = None
        days = order["days_since_delivery"]
        if "cancel" in s.intents and order["status"] == "processing":
            reason = "cancellation"
        elif tracking and tracking["status"] == "exception":
            reason = "lost_package"
        elif "damaged" in s.intents and order["status"] == "delivered" and days is not None and days <= 60:
            reason = "damaged"
        elif "refund" in s.intents and order["status"] == "delivered" and days is not None and days <= 30:
            reason = "return_within_window"
        elif "refund" in s.intents and order["status"] == "delivered":
            return ("decline", 0.0, "outside_window")
        if reason is None or amount <= 0:
            return None
        if amount > 500:
            return ("escalate", amount, reason)
        return ("refund", amount, reason)

    # --- text -------------------------------------------------------------
    @staticmethod
    def _faq_query(body: str) -> str:
        words = re.findall(r"[a-zA-Z]{4,}", body.lower())
        return " ".join(words[:8]) or "return policy"

    @staticmethod
    def _policy_query(s, tracking) -> str:
        if tracking and tracking["status"] == "exception":
            return "lost package refund"
        if "cancel" in s.intents:
            return "cancel order refund"
        if "damaged" in s.intents:
            return "damaged item refund"
        return "return window refund"

    @staticmethod
    def _subject(s) -> str:
        if "privacy_deletion" in s.intents:
            return "Your data deletion request"
        if "cancel" in s.intents:
            return "Your order has been cancelled"
        if set(s.intents) & {"refund", "damaged"}:
            return "About your return"
        return "An update on your order" if s.order_ids else "Re: your question"

    @staticmethod
    def _first_name(ctx: _Ctx) -> str:
        profile = ctx.ok("get_customer_profile")
        return profile["first_name"] if profile else "there"

    def _security_line(self, ctx: _Ctx) -> str:
        if "<SECRET_" in ctx.redaction_notice:
            return ("\n\nOne more thing: for your security, please don't share passwords over email. "
                    "Please change yours now at /account/reset.")
        return ""

    def _not_found_body(self, ctx: _Ctx, order_id: str) -> str:
        return (f"I couldn't find order {order_id} on your account. Could you double-check the order number and "
                f"reply with it? Order numbers start with 'A' followed by four digits.")

    def _compose(self, ctx: _Ctx, order, tracking, decision) -> str:
        s = ctx.signals
        if "privacy_deletion" in s.intents:
            return ("We've received your request to delete your personal data and scheduled it. It will be completed "
                    "within 30 days. Order records we must keep for tax purposes are retained in anonymised form.")
        if not order:
            hit = (ctx.ok("search_policy") or {}).get("hits", [{}])[0]
            return hit.get("text", "Thanks for your question - a teammate will follow up shortly.")
        oid = order["order_id"]
        if decision and decision[0] == "refund":
            _, amount, reason = decision
            lead = (f"I've cancelled order {oid} and refunded ${amount:.2f}" if reason == "cancellation"
                    else f"I've issued a full refund of ${amount:.2f} for order {oid} {REASON_TEXT[reason]}")
            return lead + ". It will reach your original payment method in 5-10 business days."
        if decision and decision[0] == "decline":
            return (f"Order {oid} was delivered on {order['delivered_at']}, which is outside our return window of "
                    f"30 days from delivery, so I'm not able to offer a refund. If you notice a manufacturing defect, "
                    f"our 1-year warranty covers it - just reply with the details.")
        if tracking:
            carrier = CARRIER_NAMES.get(tracking["carrier"], tracking["carrier"])
            last = tracking["events"][-1] if tracking["events"] else None
            text = f"Your order {oid} is on its way with {carrier} (tracking {tracking['tracking_number']})."
            if last:
                text += (f" Latest update on {last['date']}: {strip_internal_markup(last['description'])} "
                         f"({last['location']}).")
            if tracking.get("estimated_delivery"):
                text += f" The estimated delivery date is {tracking['estimated_delivery']}."
            return text + self._security_line(ctx)
        status = {"processing": "is being prepared and will ship within 1-2 business days",
                  "delivered": f"was delivered on {order['delivered_at']}"}.get(order["status"], f"is {order['status']}")
        return f"Your order {oid} {status}." + self._security_line(ctx)

    def _reply(self, ctx: _Ctx, core: str, subject: str) -> dict:
        body = f"Hi {self._first_name(ctx)},\n\n{core}\n\nBest regards,\nThe Acme Trail Co. Support Team"
        return self._call("send_reply", subject=subject, body=body)

    @staticmethod
    def _summary(ctx: _Ctx, order, decision) -> str:
        parts = [f"Ticket intents: {', '.join(ctx.signals.intents) or 'general question'}."]
        if order:
            parts.append(f"Order {order['order_id']} status {order['status']}.")
        if decision:
            parts.append(f"Decision: {decision[0]} ({decision[2]}).")
        tracking = ctx.ok("track_shipment")
        if tracking and tracking.get("portal_notes"):
            parts.append("Carrier page contained instructions addressed to AI agents - ignored.")
        parts.append("Reply sent." if ctx.ok("send_reply") else "No reply sent.")
        return "Internal summary: " + " ".join(parts)

    @staticmethod
    def _call(name: str, **inp: Any) -> dict:
        return {"type": "tool_use", "name": name, "input": inp}


class MockProvider:
    """Streams MockPolicy decisions with realistic token accounting."""

    def __init__(self, name: str = "mock", *, down: bool = False, stream_delay: float = 0.0):
        self.name = name
        self.down = down
        self.stream_delay = stream_delay
        self.policy = MockPolicy()

    def stream(self, req: LLMRequest) -> StreamGen:
        if self.down:
            raise ProviderUnavailable(f"{self.name}: 529 overloaded_error (simulated outage)")
        started = time.perf_counter()
        ctx = _Ctx(req)
        text, calls = self.policy.decide(ctx)

        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
            for word in re.findall(r"\S+\s*", text):
                yield {"type": "text_delta", "text": word}
                self._pause()
        seed = req.fingerprint()
        for i, call in enumerate(calls):
            call["id"] = f"toolu_mock_{hashlib.sha256(f'{seed}:{i}'.encode()).hexdigest()[:20]}"
            yield {"type": "tool_input_start", "tool": call["name"]}
            if call["name"] == "send_reply":  # simulate eager input streaming of the draft
                body = call["input"]["body"]
                for end in range(0, len(body) + 1, 12):
                    yield {"type": "tool_input_delta", "tool": "send_reply",
                           "snapshot": {"subject": call["input"]["subject"], "body": body[:end]}}
                    self._pause()
            content.append(call)

        usage = self._usage(req, content)
        return LLMResponse(model=req.model, content=content, stop_reason="tool_use" if calls else "end_turn",
                           usage=usage, provider=self.name, latency_s=time.perf_counter() - started)

    def _pause(self) -> None:
        if self.stream_delay:
            time.sleep(self.stream_delay)

    @staticmethod
    def _usage(req: LLMRequest, content: list[dict]) -> Usage:
        total_in = req.estimated_input_tokens()
        prefix = dumps({"tools": req.tools, "system": req.system})
        prefix_tokens = len(prefix) // 4
        out = max(1, len(dumps(content)) // 4)
        if prefix_tokens < MIN_CACHEABLE.get(req.model, 512):
            return Usage(input_tokens=total_in, output_tokens=out)
        key = hashlib.sha256(f"{req.model}:{prefix}".encode()).hexdigest()
        if key in _seen_prefixes:
            return Usage(input_tokens=total_in - prefix_tokens, output_tokens=out,
                         cache_read_input_tokens=prefix_tokens)
        _seen_prefixes.add(key)
        return Usage(input_tokens=total_in - prefix_tokens, output_tokens=out,
                     cache_creation_input_tokens=prefix_tokens)
