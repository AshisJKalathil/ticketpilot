"""Optional LLM-as-judge for reply quality (live runs only).

Deterministic scorers check facts and safety; the judge checks what string
matching cannot - is the reply accurate to the tool results, on-policy, and
on-brand? It is a *soft* score: it informs, it does not gate deploys alone.
"""

from __future__ import annotations

import json

import anthropic
from pydantic import BaseModel, Field

from evals.scorers import Score
from ticketpilot.config import Settings
from ticketpilot.prompts import PromptRegistry


class Verdict(BaseModel):
    accuracy: int = Field(ge=1, le=5)
    policy_adherence: int = Field(ge=1, le=5)
    tone: int = Field(ge=1, le=5)
    rationale: str


def make_judge(settings: Settings):
    client = anthropic.Anthropic()
    prompt = PromptRegistry(settings.prompts_dir).get("reply_judge")

    def judge(case: dict, outcome) -> Score:
        facts = [{"tool": c["name"], "output": c.get("output")} for c in outcome.tool_calls if c.get("ok")]
        message = prompt.render(
            ticket=json.dumps(case["ticket"], indent=2),
            tool_results=json.dumps(facts, indent=2, default=str),
            reply=outcome.final_reply or "",
        )
        response = client.messages.parse(
            model=settings.reasoning_model,
            max_tokens=4000,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": message}],
            output_format=Verdict,
        )
        v = response.parsed_output
        if v is None:
            return Score("reply_quality", False, f"judge returned no verdict (stop_reason={response.stop_reason})",
                         hard=False)
        worst = min(v.accuracy, v.policy_adherence, v.tone)
        return Score("reply_quality", worst >= 4, f"acc={v.accuracy} policy={v.policy_adherence} tone={v.tone}: "
                     f"{v.rationale[:200]}", hard=False, value=worst / 5)

    return judge
