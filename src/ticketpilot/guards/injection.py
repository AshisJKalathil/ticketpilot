"""Treat every ticket, web page and document as possible prompt injection.

Defence in depth - no single layer is trusted to be sufficient:

1. **Sanitise**: strip zero-width / bidi control characters used to hide text.
2. **Detect**: a weighted rule set scores instruction-like content. Detection
   is a *signal* for traces, approvals and routing, never the only barrier.
3. **Spotlight**: untrusted text is wrapped in ``<untrusted source=...>`` tags
   that the system prompt explains, with tag-breaking sequences neutralised.
4. **Capability reduction** (in the agent): a ticket that scores as an attack
   loses its write-scoped tools for the run, so even a successful injection has
   nothing dangerous to call. Every sensitive action is also human-approved.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

HIDDEN_CHARS = re.compile("[​-‏‪-‮⁠-⁤﻿]")

# (weight, name, pattern). Weights are summed per distinct rule, capped at 1.0.
RULES: list[tuple[float, str, re.Pattern]] = [
    (0.6, "override_instructions", re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|all|earlier|your|these)\b.{0,25}"
        r"\b(instructions?|prompts?|rules|directions|guidelines)\b", re.I | re.S)),
    (0.5, "role_hijack", re.compile(
        r"\byou are now\b|\b(admin|developer|god|debug|dan)\s+mode\b|\bact as (an? )?(admin|developer|system)\b", re.I)),
    (0.5, "system_prompt_exfiltration", re.compile(
        r"\b(system prompt|initial prompt|hidden prompt|your (instructions|guidelines|configuration))\b", re.I)),
    (0.4, "addressed_to_ai", re.compile(
        r"\b(ai|automated|llm) (assistants?|agents?|models?|systems?)\b|\b(note|notice|message) to (the )?(ai|assistant|llm)\b"
        r"|\bdear (ai|assistant|claude|chatgpt)\b", re.I)),
    (0.4, "credential_request", re.compile(
        r"\b(include|send|reveal|print|share|tell me|output)\b.{0,40}\b(password|api[ _-]?key|credentials?|tokens?|secrets?)\b",
        re.I | re.S)),
    (0.3, "authority_claim", re.compile(
        r"\b(this (instruction|request) is authori[sz]ed|by order of|admin(istrator)? override|security team requires"
        r"|authori[sz]ed by)\b", re.I)),
    (0.3, "mass_action", re.compile(
        r"\b(refund|delete|cancel|erase)\b.{0,30}\b(every|all)\b.{0,25}\b(orders?|customers?|accounts?|records?)\b"
        r"|\b(every|all)\b.{0,20}\b(orders?|customers?|accounts?)\b.{0,30}\b(refund|delete|cancel)", re.I | re.S)),
    (0.3, "tool_markup", re.compile(
        r"</?(tool_use|tool_result|function_calls?|invoke|system|untrusted)\b|\[\[?(system|assistant)\]?\]", re.I)),
    (0.3, "exfil_redirect", re.compile(r"\b(cc|bcc|forward|send a copy)\b.{0,40}(@|<EMAIL_)", re.I | re.S)),
]

FLAG_THRESHOLD = 0.5


@dataclass
class InjectionReport:
    source: str
    score: float = 0.0
    matches: list[str] = field(default_factory=list)
    hidden_chars: int = 0

    @property
    def flagged(self) -> bool:
        return self.score >= FLAG_THRESHOLD

    def as_dict(self) -> dict:
        return {"source": self.source, "score": round(self.score, 2), "matches": self.matches,
                "hidden_chars": self.hidden_chars, "flagged": self.flagged}


def sanitize(text: str) -> tuple[str, int]:
    hidden = len(HIDDEN_CHARS.findall(text))
    return unicodedata.normalize("NFKC", HIDDEN_CHARS.sub("", text)), hidden


def scan(text: str, source: str) -> InjectionReport:
    clean, hidden = sanitize(text)
    report = InjectionReport(source=source, hidden_chars=hidden)
    for weight, name, pattern in RULES:
        if pattern.search(clean):
            report.matches.append(name)
            report.score += weight
    if hidden:
        report.matches.append("hidden_unicode")
        report.score += 0.2
    report.score = min(report.score, 1.0)
    return report


def wrap_untrusted(text: str, source: str) -> str:
    """Spotlight untrusted text so the model can tell data from instructions."""
    clean, _ = sanitize(text)
    clean = re.sub(r"<\s*/?\s*untrusted", lambda m: m.group(0).replace("<", "&lt;"), clean, flags=re.I)
    return f'<untrusted source="{source}">\n{clean}\n</untrusted>'


_INTERNAL_OPEN = re.compile(r"<(untrusted|security_notice|redaction_notice|context|ticket)\b[^>]*>\n?", re.I)
_INTERNAL_CLOSE = re.compile(r"\n?</(untrusted|security_notice|redaction_notice|context|ticket)>", re.I)


def strip_internal_markup(text: str) -> str:
    """Remove harness markup (spotlight tags, notices) from text bound for a human."""
    return _INTERNAL_CLOSE.sub("", _INTERNAL_OPEN.sub("", text))
