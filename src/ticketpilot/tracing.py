"""Log every model call, tool call and decision as a trace.

Spans form a tree per run (trace_id == run_id). Attribute names follow the
OpenTelemetry GenAI semantic conventions where one exists
(``gen_ai.request.model``, ``gen_ai.usage.input_tokens`` ...), so the JSONL
export can be shipped to any OTel-compatible backend without renaming.

Only redacted data is ever written to a span.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ticketpilot.store import Store, dumps


@dataclass
class Span:
    trace_id: str
    name: str
    kind: str  # run | llm | tool | guard | decision | approval | cache
    parent_id: str | None = None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    start_ts: float = field(default_factory=time.time)
    end_ts: float | None = None
    status: str = "ok"
    attrs: dict[str, Any] = field(default_factory=dict)

    def set(self, **attrs: Any) -> Span:
        self.attrs.update(attrs)
        return self

    def event(self, name: str, **attrs: Any) -> None:
        self.attrs.setdefault("events", []).append({"name": name, "ts": time.time(), **attrs})

    def error(self, exc: BaseException | str) -> None:
        self.status = "error"
        self.attrs["error"] = str(exc)[:500]

    def as_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id, "trace_id": self.trace_id, "parent_id": self.parent_id,
            "name": self.name, "kind": self.kind, "start_ts": self.start_ts, "end_ts": self.end_ts,
            "duration_ms": round(((self.end_ts or time.time()) - self.start_ts) * 1000, 1),
            "status": self.status, "attrs": self.attrs,
        }


class Tracer:
    """One tracer per run segment.

    The parent span is tracked on an explicit per-tracer stack rather than a
    ``ContextVar``: the agent is a generator that web servers resume from
    different worker threads, and a context variable set before a ``yield``
    cannot be reset after it in another thread's context.
    """

    def __init__(self, store: Store, trace_id: str):
        self.store = store
        self.trace_id = trace_id
        self.jsonl_path = os.environ.get("TICKETPILOT_TRACE_JSONL")
        self._stack: list[Span] = []

    @contextmanager
    def span(self, name: str, kind: str, **attrs: Any) -> Iterator[Span]:
        parent = self._stack[-1] if self._stack else None
        span = Span(trace_id=self.trace_id, name=name, kind=kind,
                    parent_id=parent.span_id if parent else None, attrs=dict(attrs))
        self._stack.append(span)
        try:
            yield span
        except BaseException as exc:
            span.error(exc)
            raise
        finally:
            self._stack.remove(span)
            span.end_ts = time.time()
            self._write(span)

    def record(self, name: str, kind: str, **attrs: Any) -> None:
        """A zero-duration span - for decisions and guard verdicts."""
        with self.span(name, kind, **attrs):
            pass

    def _write(self, span: Span) -> None:
        self.store.execute(
            "INSERT OR REPLACE INTO spans(span_id,trace_id,parent_id,name,kind,start_ts,end_ts,status,attrs_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (span.span_id, span.trace_id, span.parent_id, span.name, span.kind, span.start_ts, span.end_ts,
             span.status, dumps(span.attrs)),
        )
        if self.jsonl_path:
            with open(self.jsonl_path, "a") as fh:
                fh.write(json.dumps(span.as_dict(), default=str) + "\n")


def load_trace(store: Store, trace_id: str) -> list[dict[str, Any]]:
    rows = store.query("SELECT * FROM spans WHERE trace_id=? ORDER BY start_ts", (trace_id,))
    out = []
    for r in rows:
        attrs = json.loads(r.pop("attrs_json"))
        r["attrs"] = attrs
        r["duration_ms"] = round(((r["end_ts"] or r["start_ts"]) - r["start_ts"]) * 1000, 1)
        out.append(r)
    return out
