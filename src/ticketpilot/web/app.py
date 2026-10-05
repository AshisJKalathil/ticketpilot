"""HTTP API + demo UI.  Run: ``uvicorn ticketpilot.web.app:create_app --factory``

Every agent interaction streams Server-Sent Events, so the reviewer watches
the agent investigate, sees the reply being drafted token by token, and gets
the approval card the moment the run pauses - no spinners.

Public-demo hardening (``TICKETPILOT_PUBLIC_DEMO=true``): per-IP rate limit,
a global daily spend cap, fixture customers only, and no real side effects
(refunds and emails go to sandbox tables).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ticketpilot import __version__
from ticketpilot.agent import Agent, state_snapshot
from ticketpilot.checkpoint import interrupted_runs
from ticketpilot.config import Settings
from ticketpilot.data.seed import seed
from ticketpilot.feedback import record_feedback
from ticketpilot.metrics import compute_metrics
from ticketpilot.schemas import Ticket
from ticketpilot.store import Store, utcnow
from ticketpilot.tracing import load_trace
from ticketpilot.web.carrier_portal import render_tracking_page

STATIC = Path(__file__).parent / "static"
TICKETS = Path(__file__).resolve().parents[1] / "data" / "tickets.jsonl"
log = logging.getLogger("ticketpilot.web")


class NewRun(BaseModel):
    ticket_id: str | None = None
    from_email: str | None = None
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=4000)


class Decision(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=500)
    edited_args: dict[str, Any] | None = None
    reviewer: str = Field(default="demo-reviewer", max_length=60)


class FeedbackIn(BaseModel):
    rating: str = Field(pattern="^(up|down)$")
    comment: str | None = Field(default=None, max_length=1000)


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def _sse(events: Iterator[dict]) -> StreamingResponse:
    def stream() -> Iterator[str]:
        try:
            for event in events:
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except Exception as exc:  # surface failures to the client instead of a silent hang
            log.exception("stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': f'{type(exc).__name__}: {exc}'[:300]})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def create_app(settings: Settings | None = None, *, auto_resume: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    seed(store)
    demo_tickets = [json.loads(line) for line in TICKETS.read_text().splitlines() if line.strip()]
    limiter = RateLimiter(settings.rate_limit_per_min)
    stream_delay = 0.012 if settings.provider == "mock" else 0.0

    def agent() -> Agent:
        return Agent(settings, store, stream_delay=stream_delay)

    app = FastAPI(title="TicketPilot", version=__version__)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def guard(request: Request) -> None:
        if not settings.public_demo:
            return
        if not limiter.allow(request.client.host if request.client else "anon"):
            raise HTTPException(429, "Rate limit reached - try again in a minute.")
        if store.spend_today(utcnow()[:10]) >= settings.daily_spend_cap_usd:
            raise HTTPException(503, "Today's demo budget is used up - come back tomorrow.")

    if auto_resume:
        def _resume_interrupted() -> None:
            for run_id in interrupted_runs(store):
                try:
                    for _ in agent().resume(run_id):
                        pass
                    log.info("resumed interrupted run %s", run_id)
                except Exception:
                    log.exception("could not resume %s", run_id)

        threading.Thread(target=_resume_interrupted, daemon=True, name="resume").start()

    @app.get("/", response_class=HTMLResponse)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def healthz() -> dict:
        store.one("SELECT 1 AS ok")
        return {"ok": True, "version": __version__, "provider": settings.provider}

    @app.get("/api/config")
    def config() -> dict:
        from ticketpilot.prompts import PromptRegistry

        customers = store.query("SELECT name, email, tier FROM customers WHERE deleted=0 ORDER BY id")
        return {"provider": settings.provider, "small_model": settings.small_model,
                "reasoning_model": settings.reasoning_model, "public_demo": settings.public_demo,
                "prompts": PromptRegistry(settings.prompts_dir).versions(), "customers": customers,
                "budget": {"max_cost_usd": settings.max_cost_usd, "max_seconds": settings.max_seconds,
                           "max_steps": settings.max_steps}, "version": __version__}

    @app.get("/api/tickets")
    def tickets() -> list[dict]:
        return demo_tickets

    @app.post("/api/runs")
    def start_run(body: NewRun, request: Request) -> StreamingResponse:
        guard(request)
        if body.ticket_id:
            raw = next((t for t in demo_tickets if t["id"] == body.ticket_id), None)
            if raw is None:
                raise HTTPException(404, "unknown demo ticket")
            ticket = Ticket(**raw)
        else:
            if not (body.from_email and body.subject and body.body):
                raise HTTPException(422, "from_email, subject and body are required")
            if settings.public_demo and not store.one("SELECT 1 FROM customers WHERE email=?", (body.from_email,)):
                raise HTTPException(422, "In the public demo, pick one of the fixture customers.")
            ticket = Ticket(id=f"T-{int(time.time() * 1000) % 10_000_000}", from_email=body.from_email,
                            subject=body.subject, body=body.body, received_at=utcnow(), channel="web")
        return _sse(agent().start(ticket))

    @app.get("/api/runs")
    def list_runs(limit: int = 30) -> list[dict]:
        return store.query("SELECT id, ticket_id, status, route, model, arm, cost_usd, steps, active_seconds, "
                           "created_at FROM runs ORDER BY created_at DESC LIMIT ?", (min(limit, 200),))

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        snap = state_snapshot(store, run_id)
        if snap is None:
            raise HTTPException(404, "unknown run")
        snap["approvals"] = agent().approvals.for_run(run_id)
        return snap

    @app.get("/api/runs/{run_id}/trace")
    def get_trace(run_id: str) -> list[dict]:
        return load_trace(store, run_id)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, request: Request) -> StreamingResponse:
        guard(request)
        return _sse(agent().resume(run_id))

    @app.get("/api/approvals")
    def pending_approvals() -> list[dict]:
        return agent().approvals.pending()

    @app.post("/api/approvals/{approval_id}")
    def decide(approval_id: str, body: Decision, request: Request) -> StreamingResponse:
        guard(request)
        a = agent()
        if a.approvals.get(approval_id) is None:
            raise HTTPException(404, "unknown approval")
        try:
            events = list_first(a.decide(approval_id, approved=body.approved, by=body.reviewer, note=body.note,
                                         edited_args=body.edited_args))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _sse(events)

    @app.post("/api/runs/{run_id}/feedback")
    def feedback(run_id: str, body: FeedbackIn) -> dict:
        try:
            return record_feedback(store, run_id, body.rating, body.comment, settings.eval_candidates_dir)
        except KeyError as exc:
            raise HTTPException(404, "unknown run") from exc

    @app.get("/api/metrics")
    def metrics() -> dict:
        return compute_metrics(store)

    @app.get("/carrier/pacificparcel/track", response_class=HTMLResponse)
    def carrier_portal(n: str) -> HTMLResponse:
        status, html = render_tracking_page(store, n)
        return HTMLResponse(html, status_code=status)

    @app.exception_handler(KeyError)
    def not_found(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)

    return app


def list_first(gen: Iterator[dict]) -> Iterator[dict]:
    """Run the generator's first step eagerly so validation errors become HTTP errors, then stream the rest."""
    first = next(gen)

    def chained() -> Iterator[dict]:
        yield first
        yield from gen

    return chained()
