import pytest

from ticketpilot.agent import Agent, SimulatedCrash
from ticketpilot.checkpoint import load
from ticketpilot.harness import run_ticket_to_completion
from ticketpilot.llm.pricing import Usage
from ticketpilot.llm.types import LLMResponse


def drain(gen):
    return list(gen)


def test_pauses_for_approval_and_resumes(store, settings, ticket):
    agent = Agent(settings, store)
    events = drain(agent.start(ticket("T-2002")))
    run_id = events[0]["run_id"]
    assert events[-1]["type"] == "awaiting_approval"
    assert load(store, run_id).status == "awaiting_approval"
    assert not store.query("SELECT * FROM refunds")  # nothing happens before a human says yes
    apr = agent.approvals.for_run(run_id)[0]
    assert apr["tool"] == "issue_refund" and apr["preview"]["amount"] == 189.0
    drain(agent.decide(apr["id"], approved=True, by="tester"))
    assert store.query("SELECT amount FROM refunds") == [{"amount": 189.0}]


def test_reviewer_edits_are_what_gets_sent(store, settings, ticket):
    agent = Agent(settings, store)
    run_id = drain(agent.start(ticket("T-2001")))[0]["run_id"]
    apr = agent.approvals.for_run(run_id)[0]
    drain(agent.decide(apr["id"], approved=True, by="tester",
                       edited_args={"subject": "Your daypack", "body": "Hi Daniel, it arrives on 2026-10-08. Thanks!"}))
    sent = store.one("SELECT subject, body, to_addr FROM outbox")
    assert sent == {"subject": "Your daypack", "body": "Hi Daniel, it arrives on 2026-10-08. Thanks!",
                    "to_addr": "daniel.okafor@example.com"}
    assert load(store, run_id).tool_call(apr["tool_use_id"])["edited_by_human"] is True


def test_crash_mid_run_resumes_without_repeating_work(store, settings, ticket):
    crashy = settings.with_overrides(fault_injection={"crash_after_tool": "issue_refund"})
    log: list[str] = []
    agent = Agent(crashy, store, request_log=log)
    run_id = drain(agent.start(ticket("T-2012")))[0]["run_id"]
    refund_apr = next(a for a in agent.approvals.for_run(run_id) if a["tool"] == "issue_refund")
    with pytest.raises(SimulatedCrash):
        drain(agent.decide(refund_apr["id"], approved=True, by="tester"))
    calls_before = len(log)
    assert load(store, run_id).status == "running"  # the process "died" mid-run

    fresh = Agent(crashy, store, request_log=log)  # a new process picks the run up
    drain(fresh.resume(run_id))
    reply = next(a for a in fresh.approvals.for_run(run_id) if a["tool"] == "send_reply")
    drain(fresh.decide(reply["id"], approved=True, by="tester"))
    assert load(store, run_id).status == "completed"
    assert len(store.query("SELECT * FROM refunds")) == 1  # never refunded twice
    assert len(log) - calls_before == 2  # only the remaining turns were paid for


def test_budget_exhaustion_stops_before_side_effects(store, settings, ticket):
    out = run_ticket_to_completion(ticket("T-2003"), settings=settings.with_overrides(max_cost_usd=0.0005),
                                   store=store)
    assert out.status == "budget_exceeded" and out.refunds == [] and out.escalations


def test_provider_outage_falls_back(store, settings, ticket):
    down = settings.with_overrides(fault_injection={"primary_provider_down": True})
    out = run_ticket_to_completion(ticket("T-2001"), settings=down, store=store)
    assert out.status == "completed" and out.served_by_fallback


def test_injection_ticket_loses_write_tools(store, settings, ticket):
    agent = Agent(settings, store)
    started = drain(agent.start(ticket("T-2011")))[0]
    assert started["tainted"] and "payments:refund" not in started["scopes"]


def test_repeat_question_is_served_from_answer_cache_for_free(store, settings, ticket):
    first = run_ticket_to_completion(ticket("T-2008"), settings=settings, store=store)
    assert first.status == "completed" and first.cost_usd > 0
    second = run_ticket_to_completion(
        ticket("T-2008", id="T-9001", from_email="priya.raman@example.com",
               body="What's the return policy if the shoes don't fit?"), settings=settings, store=store)
    assert second.status == "completed" and second.cost_usd == 0 and second.steps == 0
    assert second.final_reply.startswith("Hi Priya,")


def test_thumbs_down_evicts_cached_answer(store, settings, ticket):
    from ticketpilot.feedback import record_feedback

    first = run_ticket_to_completion(ticket("T-2008"), settings=settings, store=store)
    res = record_feedback(store, first.run_id, "down", "too vague", settings.eval_candidates_dir)
    assert res["evicted_cache_entries"] == 1 and settings.eval_candidates_dir.joinpath(f"{first.run_id}.json").exists()


class ScriptedProvider:
    """Returns invalid lookup_order input twice, then gives up - to test model escalation."""
    name = "scripted"

    def __init__(self):
        self.models: list[str] = []

    def stream(self, req):
        self.models.append(req.model)
        if len(self.models) <= 2:
            content = [{"type": "tool_use", "id": f"toolu_{len(self.models)}", "name": "lookup_order",
                        "input": {"order_id": "1002"}}]
            return LLMResponse(req.model, content, "tool_use", Usage(100, 10), self.name)
        return LLMResponse(req.model, [{"type": "text", "text": "done"}], "end_turn", Usage(100, 10), self.name)
        yield  # pragma: no cover - makes this a generator


def test_small_model_escalates_to_reasoning_model_after_invalid_calls(store, settings, ticket):
    provider = ScriptedProvider()
    drain(Agent(settings, store, provider=provider).start(ticket("T-2001")))
    assert provider.models == ["claude-haiku-4-5", "claude-haiku-4-5", "claude-opus-5-5"]
