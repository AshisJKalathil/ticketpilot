"""Route easy requests to a small model and hard ones to a reasoning model.

Two stages, cheapest first:

1. A transparent heuristic score from ticket signals (free, ~0 ms). Clear
   cases are decided here and the reasons are logged in the trace.
2. Only for the ambiguous middle band, a classifier on the *small* model
   decides (one short structured-output call). If it fails for any reason we
   route hard - a wrong answer costs more than the extra tokens.

The agent can also escalate mid-run from the small to the reasoning model if
the small model repeatedly produces invalid tool calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from ticketpilot.config import Settings
from ticketpilot.intents import TicketSignals
from ticketpilot.llm.pricing import Usage
from ticketpilot.prompts import PromptRegistry

EASY_MAX = 0.25
HARD_MIN = 0.5


class RouteVerdict(BaseModel):
    route: Literal["easy", "hard"]
    reason: str


@dataclass
class RouteDecision:
    route: str
    model: str
    effort: str | None
    score: float
    reasons: list[str] = field(default_factory=list)
    decided_by: str = "heuristic"  # heuristic | classifier | fail_safe | experiment
    classifier_usage: Usage | None = None

    def as_dict(self) -> dict:
        return {"route": self.route, "model": self.model, "effort": self.effort, "score": round(self.score, 2),
                "reasons": self.reasons, "decided_by": self.decided_by}


def heuristic_score(signals: TicketSignals, *, tier: str | None, injection_flagged: bool) -> tuple[float, list[str]]:
    score, reasons = 0.0, []

    def add(weight: float, why: str) -> None:
        nonlocal score
        score += weight
        reasons.append(f"{why} ({weight:+.2f})")

    if injection_flagged:
        add(0.6, "possible prompt injection")
    if "legal" in signals.intents:
        add(0.6, "legal/chargeback language")
    if "damaged" in signals.intents:
        add(0.3, "damaged goods")
    if "privacy_deletion" in signals.intents:
        add(0.3, "privacy request")
    if tier == "vip":
        add(0.3, "VIP customer")
    if signals.negative:
        add(0.2, "negative sentiment")
    if signals.urgent:
        add(0.15, "urgency/deadline")
    if len(signals.order_ids) > 1:
        add(0.2, "multiple orders")
    if len(signals.intents) > 2:
        add(0.2, "several intents")
    if any(a > 300 for a in signals.amounts):
        add(0.3, "high-value amount")
    if signals.length > 800:
        add(0.2, "long ticket")
    return score, reasons


class Router:
    def __init__(self, settings: Settings, prompts: PromptRegistry, client=None):
        self.settings = settings
        self.prompts = prompts
        self._client = client

    def decide(self, signals: TicketSignals, *, text: str, tier: str | None, injection_flagged: bool) -> RouteDecision:
        score, reasons = heuristic_score(signals, tier=tier, injection_flagged=injection_flagged)
        decided_by, usage = "heuristic", None
        if score >= HARD_MIN:
            route = "hard"
        elif score <= EASY_MAX:
            route = "easy"
        elif self.settings.provider == "anthropic":
            route, why, usage = self._classify(text)
            decided_by = "classifier" if why else "fail_safe"
            reasons.append(f"classifier: {why or 'unavailable - fail safe to hard'}")
        else:
            route, decided_by = "hard", "fail_safe"
            reasons.append("ambiguous - fail safe to hard")
        return self._decision(route, score, reasons, decided_by, usage)

    def _decision(self, route, score, reasons, decided_by, usage) -> RouteDecision:
        hard = route == "hard"
        return RouteDecision(
            route=route,
            model=self.settings.reasoning_model if hard else self.settings.small_model,
            effort=self.settings.reasoning_effort if hard else None,
            score=score, reasons=reasons, decided_by=decided_by, classifier_usage=usage,
        )

    def _classify(self, text: str) -> tuple[str, str | None, Usage | None]:
        try:
            import anthropic

            client = self._client or anthropic.Anthropic(max_retries=1, timeout=15.0)
            response = client.messages.parse(
                model=self.settings.small_model,
                max_tokens=300,
                messages=[{"role": "user", "content": self.prompts.get("router_classifier").render(ticket=text)}],
                output_format=RouteVerdict,
            )
            u = response.usage
            usage = Usage(u.input_tokens or 0, u.output_tokens or 0)
            verdict = response.parsed_output
            if verdict is None:
                return "hard", None, usage
            return verdict.route, verdict.reason[:200], usage
        except Exception:  # any classifier failure -> fail safe
            return "hard", None, None
