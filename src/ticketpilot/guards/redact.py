"""Strip secrets and PII before anything reaches the model.

Sensitive values are *tokenised*, not deleted: ``maya@example.com`` becomes
``<EMAIL_1>`` and the mapping lives in a per-run :class:`Vault`. The model
reasons over placeholders; tools that legitimately need the real value (e.g.
sending the email) rehydrate it *after* the model, inside the tool sandbox.
Traces, evals and fine-tuning exports only ever see the placeholder form.

Customer first names are intentionally kept so replies can greet people; set
the policy per deployment.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_card(match: str) -> bool:
    digits = re.sub(r"\D", "", match)
    return 13 <= len(digits) <= 19 and _luhn_ok(digits)


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern
    group: int = 0
    check: Callable[[str], bool] | None = None


# Order matters: secrets and cards before phones (long digit runs), emails early.
RULES: list[Rule] = [
    Rule("SECRET", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    Rule("SECRET", re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    Rule("SECRET", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    Rule("SECRET", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    Rule("SECRET", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    Rule("SECRET", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    Rule("SECRET", re.compile(
        r"(?i)\b(?:password|passwd|pwd|passcode|pin)\b\s*(?:is|was|:|=)?\s*[\"'“]?([^\s\"'”,;)]{4,})"), group=1),
    Rule("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    Rule("CARD", re.compile(r"(?<!\w)(?:\d[ -]?){12,18}\d(?!\w)"), check=_is_card),
    Rule("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    Rule("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b")),
    Rule("PHONE", re.compile(r"(?<![\w+])(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}(?!\w)")),
]

PLACEHOLDER = re.compile(r"<(SECRET|EMAIL|CARD|SSN|IBAN|PHONE)_(\d+)>")


@dataclass
class Vault:
    """Per-run mapping between placeholders and real values."""

    forward: dict[str, str] = field(default_factory=dict)  # real -> placeholder
    counters: dict[str, int] = field(default_factory=dict)

    def token_for(self, kind: str, value: str) -> str:
        if value in self.forward:
            return self.forward[value]
        n = self.counters.get(kind, 0) + 1
        self.counters[kind] = n
        token = f"<{kind}_{n}>"
        self.forward[value] = token
        return token

    @property
    def reverse(self) -> dict[str, str]:
        return {v: k for k, v in self.forward.items()}

    def to_dict(self) -> dict:
        return {"forward": self.forward, "counters": self.counters}

    @classmethod
    def from_dict(cls, data: dict | None) -> Vault:
        data = data or {}
        return cls(forward=dict(data.get("forward", {})), counters=dict(data.get("counters", {})))


@dataclass
class RedactionResult:
    text: str
    findings: list[str]


def redact(text: str, vault: Vault) -> RedactionResult:
    findings: list[str] = []
    for rule in RULES:
        def _sub(m: re.Match, rule: Rule = rule) -> str:
            value = m.group(rule.group)
            if PLACEHOLDER.fullmatch(value) or (rule.check and not rule.check(value)):
                return m.group(0)
            findings.append(rule.kind)
            token = vault.token_for(rule.kind, value)
            whole = m.group(0)
            if rule.group:
                start = m.start(rule.group) - m.start(0)
                return whole[:start] + token + whole[start + len(value):]
            return token

        text = rule.pattern.sub(_sub, text)
    return RedactionResult(text, findings)


def redact_obj(obj: Any, vault: Vault, findings: list[str] | None = None) -> Any:
    """Recursively redact every string inside dicts/lists."""
    findings = findings if findings is not None else []
    if isinstance(obj, str):
        r = redact(obj, vault)
        findings.extend(r.findings)
        return r.text
    if isinstance(obj, dict):
        return {k: redact_obj(v, vault, findings) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, vault, findings) for v in obj]
    return obj


def rehydrate(text: str, vault: Vault) -> str:
    reverse = vault.reverse
    return PLACEHOLDER.sub(lambda m: reverse.get(m.group(0), m.group(0)), text)


def rehydrate_obj(obj: Any, vault: Vault) -> Any:
    if isinstance(obj, str):
        return rehydrate(obj, vault)
    if isinstance(obj, dict):
        return {k: rehydrate_obj(v, vault) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rehydrate_obj(v, vault) for v in obj]
    return obj
