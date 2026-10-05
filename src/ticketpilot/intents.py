"""Cheap, deterministic signals extracted from a (redacted) ticket.

Used for routing, least-privilege tool grants and the FAQ answer cache. These
are *hints* - the model still makes the decisions - so they are tuned to be
conservative (when in doubt: route hard, grant less).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ORDER_ID = re.compile(r"\bA\d{4}\b")
MONEY = re.compile(r"\$\s?(\d{1,6}(?:\.\d{2})?)")

INTENT_PATTERNS: dict[str, re.Pattern] = {
    "privacy_deletion": re.compile(
        r"\b(delete|erase|remove|wipe)\b.{0,40}\b(personal )?(data|account|information|details)\b"
        r"|\bgdpr\b|\bccpa\b|right to be forgotten", re.I | re.S),
    "legal": re.compile(
        r"\b(chargeback|lawyer|attorney|sue|legal action|small claims|bbb|dispute (the|this) charge)\b", re.I),
    "cancel": re.compile(r"\bcancel(l?ed|l?ing|lation)?\b", re.I),
    "damaged": re.compile(r"\b(damaged|broken|defective|torn|ripped|rip|cracked|leaks?|leaking|faulty)\b", re.I),
    "refund": re.compile(r"\b(refund|return|money back|send it back)\b", re.I),
    "where_is_order": re.compile(
        r"\b(where is|where's|hasn'?t arrived|has not arrived|never arrived|not arrived|tracking|delivery status"
        r"|status of|when will|still waiting|haven'?t received|update on|check on)\b", re.I),
}

NEGATIVE = re.compile(r"\b(ridiculous|unacceptable|furious|disappointed|terrible|worst|angry|outraged|disgusted)\b", re.I)
URGENT = re.compile(r"\b(urgent|asap|immediately|next weekend|tomorrow|need (these|this|it))\b", re.I)

FAQ_INTENTS = {"refund", "where_is_order"}


@dataclass
class TicketSignals:
    order_ids: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    negative: bool = False
    urgent: bool = False
    amounts: list[float] = field(default_factory=list)
    length: int = 0

    @property
    def general_question(self) -> bool:
        """No account-specific ask: safe to answer from the KB and to cache."""
        return not self.order_ids and set(self.intents) <= FAQ_INTENTS

    def as_dict(self) -> dict:
        return {**self.__dict__, "general_question": self.general_question}


def analyze(text: str) -> TicketSignals:
    return TicketSignals(
        order_ids=list(dict.fromkeys(ORDER_ID.findall(text))),
        intents=[name for name, pat in INTENT_PATTERNS.items() if pat.search(text)],
        negative=bool(NEGATIVE.search(text)),
        urgent=bool(URGENT.search(text)),
        amounts=[float(a) for a in MONEY.findall(text)],
        length=len(text),
    )


BASE_SCOPES = {"orders:read", "shipping:read", "kb:read", "customer:read", "memory:write", "email:send", "escalate"}


def least_privilege_scopes(signals: TicketSignals, *, tainted: bool, known_customer: bool) -> set[str]:
    """Grant only what this ticket plausibly needs.

    * refunds only when the ticket is about an order problem;
    * data deletion only when the customer asked for it;
    * a ticket that looks like an injection attack loses every write scope
      except replying and escalating;
    * an unknown sender gets the knowledge base and escalation only.
    """
    if not known_customer:
        return {"kb:read", "email:send", "escalate"}
    scopes = set(BASE_SCOPES)
    if set(signals.intents) & {"refund", "cancel", "damaged", "where_is_order"} and signals.order_ids:
        scopes.add("payments:refund")
    if "privacy_deletion" in signals.intents:
        scopes.add("privacy:delete")
    if tainted:
        scopes -= {"payments:refund", "privacy:delete", "memory:write"}
    return scopes
