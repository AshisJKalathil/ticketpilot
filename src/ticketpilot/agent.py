"""The TicketPilot agent: intake -> route -> tool loop -> approvals -> finish.

The agent is a *generator of events*: every public method yields dicts
(``text_delta``, ``tool_call``, ``tool_result``, ``approval_required``,
``run_finished`` ...) that the web UI streams over SSE and the CLI prints. A
run pauses at an approval and continues later in a different request or
process from its checkpoint.

    agent = Agent(settings, store)
    for event in agent.start(ticket): ...
    for event in agent.decide(approval_id, approved=True, by="sam"): ...
    for event in agent.resume(run_id): ...          # after a crash
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Generator
from dataclasses import asdict
from typing import Any

from pydantic import ValidationError

from ticketpilot.approvals import ApprovalService, build_preview, risk_notes
from ticketpilot.budget import Budget, BudgetExceeded
from ticketpilot.cache import AnswerCache
from ticketpilot.checkpoint import RunState, load, save
from ticketpilot.config import Settings
from ticketpilot.experiments import assign, load_experiments
from ticketpilot.guards.injection import sanitize, scan, wrap_untrusted
from ticketpilot.guards.redact import PLACEHOLDER, Vault, redact
from ticketpilot.intents import analyze, least_privilege_scopes
from ticketpilot.llm.fallback import build_provider
from ticketpilot.llm.router import Router
from ticketpilot.llm.types import (
    LLMRequest,
    LLMResponse,
    MalformedToolInput,
    Provider,
    ProviderFatal,
    ProviderUnavailable,
)
from ticketpilot.prompts import PromptRegistry
from ticketpilot.schemas import RunStatus, Ticket
from ticketpilot.store import Store, dumps, utcnow
from ticketpilot.tools import kb
from ticketpilot.tools.base import ToolContext
from ticketpilot.tools.catalog import render_outgoing
from ticketpilot.tools.registry import ToolOutcome, ToolRegistry
from ticketpilot.tracing import Tracer

Events = Generator[dict[str, Any], None, Any]

MAX_TOKENS = {"easy": 4096, "hard": 16000}


class SimulatedCrash(RuntimeError):
    """Raised by the ``crash_after_tool`` fault-injection hook (tests only)."""


class Agent:
    def __init__(self, settings: Settings, store: Store, *, provider: Provider | None = None,
                 registry: ToolRegistry | None = None, prompts: PromptRegistry | None = None,
                 request_log: list[str] | None = None, stream_delay: float = 0.0):
        self.settings = settings
        self.store = store
        self.provider = provider or build_provider(settings, stream_delay=stream_delay)
        self.registry = registry or ToolRegistry()
        self.prompts = prompts or PromptRegistry(settings.prompts_dir)
        self.router = Router(settings, self.prompts)
        self.approvals = ApprovalService(store)
        self.experiments = load_experiments(settings.experiments_path) if settings.experiments_enabled else []
        self.request_log = request_log

    # ------------------------------------------------------------------ API
    def start(self, ticket: Ticket) -> Events:
        state = self._intake(ticket)
        yield self._started_event(state)
        yield from self._drive(state)

    def resume(self, run_id: str) -> Events:
        state = self._load(run_id)
        if RunStatus(state.status).terminal:
            yield self._finished_event(state)
            return
        if state.status == RunStatus.AWAITING_APPROVAL and self._waiting_on(state):
            yield self._awaiting_event(state)
            return
        state.status = RunStatus.RUNNING.value
        yield {"type": "run_resumed", "run_id": run_id}
        yield from self._drive(state)

    def decide(self, approval_id: str, *, approved: bool, by: str, note: str | None = None,
               edited_args: dict | None = None) -> Events:
        apr = self.approvals.decide(approval_id, approved=approved, by=by, note=note, edited_args=edited_args)
        Tracer(self.store, apr["run_id"]).record(
            "approval.decided", "approval", approval_id=approval_id, tool=apr["tool"], status=apr["status"],
            decided_by=by, edited=bool(edited_args), note=note)
        yield {"type": "approval_decided", "approval_id": approval_id, "tool": apr["tool"], "status": apr["status"],
               "run_id": apr["run_id"]}
        yield from self.resume(apr["run_id"])

    # --------------------------------------------------------------- intake
    def _intake(self, ticket: Ticket) -> RunState:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        tracer = Tracer(self.store, run_id)
        budget = Budget(self.settings.max_cost_usd, self.settings.max_seconds, self.settings.max_steps,
                        self.settings.max_total_tokens)
        with tracer.span("run.intake", "run", ticket_id=ticket.id, channel=ticket.channel):
            customer = self.store.one("SELECT * FROM customers WHERE lower(email)=lower(?) AND deleted=0",
                                      (ticket.from_email,))
            vault = Vault()
            sender = vault.token_for("EMAIL", ticket.from_email)

            injection = scan(f"{ticket.subject}\n{ticket.body}", "ticket")
            subject = redact(sanitize(ticket.subject)[0], vault)
            body = redact(sanitize(ticket.body)[0], vault)
            findings = subject.findings + body.findings
            signals = analyze(f"{subject.text}\n{body.text}")
            tainted = injection.flagged
            scopes = least_privilege_scopes(signals, tainted=tainted, known_customer=customer is not None)
            tracer.record("guard.pii_redaction", "guard", findings=dict(Counter(findings)))
            tracer.record("guard.injection_scan", "guard", **injection.as_dict())
            tracer.record("decision.scopes", "decision", scopes=sorted(scopes), tainted=tainted,
                          signals=signals.as_dict())

            tier = customer["tier"] if customer else None
            route = self.router.decide(signals, text=f"Subject: {subject.text}\n\n{body.text}", tier=tier,
                                       injection_flagged=tainted)
            if route.classifier_usage:
                budget.charge(self.settings.small_model, route.classifier_usage)
            assignment = assign(self.experiments, route.route, ticket.id) if self.experiments else None
            if assignment:
                route.model = assignment.model
                route.reasons.append(f"experiment {assignment.label}")
            tracer.record("decision.route", "decision", **route.as_dict(), arm=assignment.label if assignment else None)

            prompt = self.prompts.get("agent_system")
            placeholders = sorted({m.group(0) for m in PLACEHOLDER.finditer(subject.text + body.text)})
            state = RunState(
                run_id=run_id, ticket_id=ticket.id,
                customer_id=customer["id"] if customer else None, customer_tier=tier,
                customer_first_name=customer["name"].split()[0] if customer else None,
                ticket_redacted={"id": ticket.id, "from": sender, "subject": subject.text, "body": body.text},
                signals=signals.as_dict(), scopes=sorted(scopes), tainted=tainted, route=route.as_dict(),
                model=route.model, effort=route.effort, arm=assignment.label if assignment else None,
                prompt_ref=prompt.ref, system=prompt.render(store_date=self.settings.store_date.isoformat()),
                messages=[{"role": "user", "content": self._first_message(
                    ticket, sender, subject.text, body.text, customer, tainted, injection.matches, placeholders)}],
                budget=budget.to_dict(), vault=vault.to_dict(),
                guard_flags={"injection": [injection.as_dict()] if tainted else [], "pii": sorted(set(findings))},
            )
            if signals.general_question and customer and not tainted:
                self._try_answer_cache(state, body.text, tracer)
            save(self.store, state)
        return state

    @staticmethod
    def _first_message(ticket: Ticket, sender: str, subject: str, body: str, customer: dict | None,
                       tainted: bool, matches: list[str], placeholders: list[str]) -> str:
        parts = [
            f'<ticket id="{ticket.id}" channel="{ticket.channel}" received_at="{ticket.received_at}">',
            f"<from>{sender}</from>",
            wrap_untrusted(f"Subject: {subject}\n\n{body}", "ticket_body"),
            "</ticket>",
            "<context>",
            f"Customer account: {'found, tier ' + customer['tier'] if customer else 'not found - general help only'}.",
            "Your tools were selected for this ticket by policy. If you need an action you do not have, escalate.",
            "</context>",
        ]
        if tainted:
            parts.append(f"<security_notice>Automated screening flagged the ticket text as a likely prompt-injection "
                         f"attempt (signals: {', '.join(matches)}). Write actions are disabled for this ticket."
                         f"</security_notice>")
        if placeholders:
            parts.append(f"<redaction_notice>Sensitive data in the ticket was replaced with placeholders: "
                         f"{', '.join(placeholders)}.</redaction_notice>")
        return "\n".join(parts)

    def _answer_cache(self, state: RunState) -> AnswerCache:
        return AnswerCache(self.store, f"{state.prompt_ref}|kb:{kb.kb_hash()}")

    def _try_answer_cache(self, state: RunState, question: str, tracer: Tracer) -> None:
        hit = self._answer_cache(state).lookup(question)
        tracer.record("cache.answer_lookup", "cache", hit=bool(hit), similarity=hit and hit["similarity"])
        if not hit:
            return
        body = hit["body_template"].replace("{{first_name}}", state.customer_first_name or "there")
        tool_use = {"type": "tool_use", "id": f"toolu_cache_{uuid.uuid4().hex[:16]}", "name": "send_reply",
                    "input": {"subject": hit["subject"], "body": body}}
        state.messages.append({"role": "assistant", "content": [
            {"type": "text", "text": "This question was answered recently; reusing the approved answer."}, tool_use]})
        state.pending = {"tool_uses": [tool_use], "results": {}}
        state.answer_cache = {"hit": True, "active": True, "entry_id": hit["entry_id"],
                              "similarity": hit["similarity"]}

    # ----------------------------------------------------------------- loop
    def _drive(self, state: RunState) -> Events:
        budget = Budget.from_dict(state.budget)
        budget.start_clock()
        tracer = Tracer(self.store, state.run_id)
        try:
            with tracer.span("run.drive", "run", model=state.model, status_in=state.status):
                while state.status == RunStatus.RUNNING:
                    if state.pending:
                        waiting = yield from self._process_pending(state, budget, tracer)
                        if waiting:
                            state.status = RunStatus.AWAITING_APPROVAL.value
                        continue
                    response = yield from self._model_turn(state, budget, tracer)
                    if response is None:
                        break
        except BudgetExceeded as exc:
            self._handoff(state, RunStatus.BUDGET_EXCEEDED, "needs_human_judgement",
                          f"Run stopped before any further action: {exc}.", tracer)
        except ProviderUnavailable as exc:
            self._handoff(state, RunStatus.FAILED, "tool_failure", f"All model providers unavailable: {exc}", tracer)
        except ProviderFatal as exc:
            self._handoff(state, RunStatus.FAILED, "tool_failure", f"Model request failed: {exc}", tracer)
        finally:
            budget.stop_clock()
            state.budget = budget.to_dict()
            self._settle_spend(state)
            save(self.store, state)
        if state.status == RunStatus.AWAITING_APPROVAL:
            yield self._awaiting_event(state)
        else:
            yield self._finished_event(state)

    def _model_turn(self, state: RunState, budget: Budget, tracer: Tracer) -> Events:
        route = state.route.get("route", "hard")
        tools = self.registry.definitions(set(state.scopes))
        request = LLMRequest(model=state.model, system=state.system, messages=state.messages, tools=tools,
                             max_tokens=MAX_TOKENS.get(route, 16000), effort=state.effort,
                             metadata={"run_id": state.run_id})
        request.max_tokens = budget.preflight(request.model, request.estimated_input_tokens(), request.max_tokens)
        response = yield from self._call_model(request, state, budget, tracer)
        budget.steps += 1
        state.messages.append({"role": "assistant", "content": response.content})
        state.budget = budget.to_dict()
        save(self.store, state)
        budget.check()  # over budget now? stop *before* running any requested tool

        tool_uses = response.tool_uses()
        if response.stop_reason == "refusal":
            self._handoff(state, RunStatus.ESCALATED, "needs_human_judgement",
                          "The model declined this request; a human should handle it.", tracer)
            return None
        if response.stop_reason == "max_tokens" and tool_uses:
            state.pending = {"tool_uses": tool_uses, "results": {
                tu["id"]: self._error_result(tu["id"], "Your output was cut off at the token limit, so this call "
                                                       "was not run. Retry with shorter input.") for tu in tool_uses}}
            return response
        if not tool_uses:
            state.summary = response.text().strip() or None
            state.status = RunStatus.COMPLETED.value
            return None
        state.pending = {"tool_uses": tool_uses, "results": {}}
        save(self.store, state)
        return response

    def _call_model(self, request: LLMRequest, state: RunState, budget: Budget, tracer: Tracer) -> Events:
        for attempt in range(1, 4):
            with tracer.span("llm.call", "llm", **{
                "gen_ai.system": "anthropic", "gen_ai.request.model": request.model,
                "gen_ai.request.max_tokens": request.max_tokens, "ticketpilot.prompt": state.prompt_ref,
                "ticketpilot.step": budget.steps + 1, "ticketpilot.attempt": attempt,
            }) as span:
                if self.request_log is not None:
                    self.request_log.append(dumps(request.payload()))
                try:
                    response: LLMResponse = yield from self._relay(self.provider.stream(request))
                except MalformedToolInput as exc:
                    span.error(exc)
                    yield {"type": "stream_reset", "reason": "malformed tool input - retrying the turn"}
                    continue
                cost = budget.charge(response.model, response.usage)
                state.served_by_fallback = state.served_by_fallback or response.served_by_fallback
                span.set(**{
                    "gen_ai.response.stop_reason": response.stop_reason, "gen_ai.provider": response.provider,
                    "gen_ai.usage.input_tokens": response.usage.input_tokens,
                    "gen_ai.usage.output_tokens": response.usage.output_tokens,
                    "gen_ai.usage.cache_read_input_tokens": response.usage.cache_read_input_tokens,
                    "gen_ai.usage.cache_creation_input_tokens": response.usage.cache_creation_input_tokens,
                    "ticketpilot.cost_usd": round(cost, 6), "served_by_fallback": response.served_by_fallback,
                    "latency_s": round(response.latency_s, 3), "request_id": response.request_id,
                    "tool_calls": [t["name"] for t in response.tool_uses()],
                })
                yield {"type": "llm_usage", "model": response.model, "provider": response.provider,
                       "usage": response.usage.as_dict(), "cost_usd": round(cost, 6),
                       "total_cost_usd": round(budget.cost_usd, 6), "step": budget.steps + 1}
                return response
        raise ProviderFatal("model produced unparseable tool input three times in a row")

    @staticmethod
    def _relay(stream) -> Events:
        """Forward provider events to the client in UI-friendly shapes; return the response."""
        while True:
            try:
                event = next(stream)
            except StopIteration as stop:
                return stop.value
            kind = event["type"]
            if kind == "tool_input_delta" and event.get("tool") == "send_reply":
                snap = event.get("snapshot") or {}
                yield {"type": "draft_delta", "subject": snap.get("subject", ""), "body": snap.get("body", "")}
            elif kind in {"text_delta", "stream_reset", "provider_fallback", "tool_input_start"}:
                yield event

    # ---------------------------------------------------------------- tools
    def _process_pending(self, state: RunState, budget: Budget, tracer: Tracer) -> Events:
        pending = state.pending
        vault = Vault.from_dict(state.vault)
        waiting = False
        ended = False
        for tu in pending["tool_uses"]:
            tid, name, args = tu["id"], tu["name"], tu.get("input") or {}
            if tid in pending["results"]:
                continue
            log = state.tool_call(tid)
            if log is None:
                log = {"id": tid, "name": name, "input": args, "executed": False, "approval": None,
                       "from_cache": bool(state.answer_cache and state.answer_cache.get("active"))}
                state.tool_calls.append(log)
                yield {"type": "tool_call", "id": tid, "name": name, "input": args}
            spec = self.registry.get(name)
            edited = False
            if spec and spec.requires_approval and spec.scopes <= set(state.scopes):
                try:
                    clean = self.registry.validate_input(spec, args)
                except ValidationError as exc:
                    outcome = ToolOutcome(name, False, error=f"Invalid input for {name}: {exc.errors()[0]['msg']}",
                                          error_kind="invalid_input")
                    outcome.model_content = json.dumps({"error": outcome.error})
                    self._record(state, log, outcome, tid, executed=False)
                    yield self._result_event(tid, name, outcome)
                    continue
                apr = self.approvals.for_tool_use(state.run_id, tid)
                if apr is None:
                    apr = self.approvals.create(
                        run_id=state.run_id, tool_use_id=tid, tool=name, args=clean,
                        preview=build_preview(name, clean, vault=vault, store=self.store,
                                              customer_id=state.customer_id),
                        risk_notes=risk_notes(name, clean, tainted=state.tainted, tier=state.customer_tier,
                                              injection_sources=[f["source"] for f in state.guard_flags["injection"]]),
                    )
                    log.update(approval="pending", approval_id=apr["id"])
                    tracer.record("approval.requested", "approval", approval_id=apr["id"], tool=name,
                                  risk_notes=apr["risk_notes"])
                    yield {"type": "approval_required", "approval_id": apr["id"], "tool": name,
                           "preview": apr["preview"], "risk_notes": apr["risk_notes"], "run_id": state.run_id}
                if apr["status"] == "pending":
                    waiting = True
                    continue
                if apr["status"] == "rejected":
                    log["approval"] = "rejected"
                    note = apr.get("decision_note") or ""
                    outcome = ToolOutcome(name, False, error=f"Rejected by reviewer: {note}", error_kind="rejected")
                    outcome.model_content = json.dumps({"status": "rejected_by_human", "note": note})
                    self._record(state, log, outcome, tid, executed=False)
                    if state.answer_cache and state.answer_cache.get("active"):
                        self.store.execute("DELETE FROM semantic_cache WHERE id=?", (state.answer_cache["entry_id"],))
                        state.answer_cache["active"] = False
                    yield self._result_event(tid, name, outcome)
                    save(self.store, state)
                    continue
                log["approval"] = "approved"
                if apr.get("edited_args"):
                    args, edited = apr["edited_args"], True
                    log["edited_by_human"] = True
                else:
                    args = clean

            outcome = self._execute(state, tid, name, args, vault, budget, tracer, edited=edited)
            self._record(state, log, outcome, tid, executed=outcome.ok or outcome.error_kind not in
                         {"permission", "invalid_input"})
            if name == "send_reply" and outcome.ok:
                state.final_reply = render_outgoing(args["body"], vault)
                self._maybe_cache_answer(state, args, edited)
            if spec and spec.ends_run and outcome.ok:
                ended = True
                state.summary = f"Escalated to {outcome.output['queue']}: {args.get('summary', '')}"
            yield self._result_event(tid, name, outcome)
            save(self.store, state)
            crash_on = self.settings.fault_injection.get("crash_after_tool")
            if crash_on == name and not state.crashed_once:
                state.crashed_once = True
                save(self.store, state)
                raise SimulatedCrash(f"simulated crash after {name}")

        if waiting:
            save(self.store, state)
            return True
        state.messages.append({"role": "user", "content": [pending["results"][tu["id"]]
                                                              for tu in pending["tool_uses"]]})
        state.pending = None
        if ended:
            state.status = RunStatus.ESCALATED.value
        elif state.answer_cache and state.answer_cache.get("active") and state.final_reply:
            state.status = RunStatus.COMPLETED.value
            state.summary = (f"Answered from the answer cache (entry {state.answer_cache['entry_id']}, "
                             f"similarity {state.answer_cache['similarity']}); no model call needed.")
        self._maybe_escalate_model(state, tracer)
        save(self.store, state)
        return False

    def _execute(self, state: RunState, tid: str, name: str, args: dict, vault: Vault, budget: Budget,
                 tracer: Tracer, *, edited: bool) -> ToolOutcome:
        ctx = ToolContext(store=self.store, settings=self.settings, run_id=state.run_id,
                          customer_id=state.customer_id, scopes=frozenset(state.scopes), tool_use_id=tid,
                          vault=vault, edited_by_human=edited)
        with tracer.span(f"tool.{name}", "tool", **{"tool.name": name, "tool.input": args}) as span:
            outcome = self.registry.execute(
                name, args, ctx, vault=vault, deadline=budget.deadline(),
                on_retry=lambda n, exc, delay: span.event("retry", attempt=n, error=str(exc)[:200],
                                                         delay_s=round(delay, 3)))
            span.set(ok=outcome.ok, attempts=outcome.attempts, cached=outcome.cached, error=outcome.error,
                     error_kind=outcome.error_kind, injection=outcome.injection)
            if not outcome.ok:
                span.status = "error"
        state.vault = vault.to_dict()
        return outcome

    def _record(self, state: RunState, log: dict, outcome: ToolOutcome, tid: str, *, executed: bool) -> None:
        log.update(outcome.as_log())
        log["executed"] = executed
        state.pending["results"][tid] = {"type": "tool_result", "tool_use_id": tid, "content": outcome.model_content,
                                         **({"is_error": True} if not outcome.ok else {})}
        if outcome.injection:
            state.guard_flags["injection"].extend(outcome.injection)
        state.invalid_streak = state.invalid_streak + 1 if outcome.error_kind == "invalid_input" else 0

    @staticmethod
    def _error_result(tid: str, message: str) -> dict:
        return {"type": "tool_result", "tool_use_id": tid, "content": json.dumps({"error": message}), "is_error": True}

    @staticmethod
    def _result_event(tid: str, name: str, outcome: ToolOutcome) -> dict:
        return {"type": "tool_result", "id": tid, "name": name, "ok": outcome.ok, "output": outcome.output,
                "error": outcome.error, "attempts": outcome.attempts, "cached": outcome.cached,
                "duration_ms": round(outcome.duration_ms, 1), "injection": outcome.injection}

    def _maybe_cache_answer(self, state: RunState, args: dict, edited: bool) -> None:
        if (edited or state.tainted or not state.signals.get("general_question")
                or (state.answer_cache and state.answer_cache.get("hit"))):
            return
        body = args["body"]
        if state.customer_first_name:
            body = body.replace(state.customer_first_name, "{{first_name}}")
        self._answer_cache(state).put(state.ticket_redacted["body"], {
            "subject": args["subject"], "body_template": body, "source_run": state.run_id})

    def _maybe_escalate_model(self, state: RunState, tracer: Tracer) -> None:
        if state.invalid_streak >= 2 and state.model == self.settings.small_model:
            tracer.record("decision.model_escalation", "decision", from_model=state.model,
                          to_model=self.settings.reasoning_model, reason="repeated invalid tool calls")
            state.model, state.effort = self.settings.reasoning_model, self.settings.reasoning_effort
            state.invalid_streak = 0

    # -------------------------------------------------------------- endings
    def _handoff(self, state: RunState, status: RunStatus, reason: str, detail: str, tracer: Tracer) -> None:
        esc_id = f"esc_{uuid.uuid4().hex[:12]}"
        self.store.execute("INSERT INTO escalations(id,run_id,reason,priority,summary,created_at) VALUES(?,?,?,?,?,?)",
                           (esc_id, state.run_id, reason, "high", detail, utcnow()))
        state.status = status.value
        state.summary = detail
        tracer.record("run.handoff", "decision", status=status.value, reason=reason, detail=detail)

    def _settle_spend(self, state: RunState) -> None:
        delta = state.budget.get("cost_usd", 0.0) - state.ledger_charged
        if delta > 0:
            self.store.add_spend(utcnow()[:10], delta)
            state.ledger_charged += delta

    def _waiting_on(self, state: RunState) -> list[dict]:
        return [a for a in self.approvals.for_run(state.run_id) if a["status"] == "pending"]

    def _load(self, run_id: str) -> RunState:
        state = load(self.store, run_id)
        if state is None:
            raise KeyError(f"unknown run {run_id}")
        return state

    # --------------------------------------------------------------- events
    @staticmethod
    def _started_event(state: RunState) -> dict:
        return {"type": "run_started", "run_id": state.run_id, "ticket_id": state.ticket_id, "route": state.route,
                "model": state.model, "arm": state.arm, "scopes": state.scopes, "tainted": state.tainted,
                "guard_flags": state.guard_flags, "prompt": state.prompt_ref, "answer_cache": state.answer_cache}

    def _awaiting_event(self, state: RunState) -> dict:
        return {"type": "awaiting_approval", "run_id": state.run_id,
                "approvals": [{"approval_id": a["id"], "tool": a["tool"], "preview": a["preview"],
                               "risk_notes": a["risk_notes"]} for a in self._waiting_on(state)],
                "metrics": self._metrics(state)}

    def _finished_event(self, state: RunState) -> dict:
        return {"type": "run_finished", "run_id": state.run_id, "status": state.status,
                "final_reply": state.final_reply, "summary": state.summary, "metrics": self._metrics(state)}

    @staticmethod
    def _metrics(state: RunState) -> dict:
        b = state.budget
        return {"route": state.route.get("route"), "model": state.model, "arm": state.arm,
                "cost_usd": round(b.get("cost_usd", 0.0), 6), "input_tokens": b.get("input_tokens", 0),
                "output_tokens": b.get("output_tokens", 0), "cache_read_tokens": b.get("cache_read_tokens", 0),
                "steps": b.get("steps", 0), "active_seconds": round(b.get("active_seconds", 0.0), 3),
                "served_by_fallback": state.served_by_fallback,
                "answer_cache_hit": bool(state.answer_cache and state.answer_cache.get("hit"))}


def state_snapshot(store: Store, run_id: str) -> dict | None:
    state = load(store, run_id)
    if state is None:
        return None
    data = asdict(state)
    data.pop("vault", None)  # never ship the PII vault to a client
    data.pop("system", None)
    return data
