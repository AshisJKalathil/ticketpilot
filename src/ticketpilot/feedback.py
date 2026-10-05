"""Turn every thumbs-down into a new eval case.

A thumbs-down (from a reviewer in the UI, or a customer CSAT hook) does three
things immediately:

1. records the rating against the run (feeds A/B reports and fine-tune
   selection);
2. evicts any answer-cache entry the run created, so a bad answer is never
   served again;
3. writes ``evals/candidates/<run_id>.json``: the redacted ticket, the full
   trajectory and the reviewer's comment, with an ``expect`` block pre-filled
   from what the agent did and marked TODO for a human to correct.

``scripts/promote_candidate.py`` turns a labelled candidate into a golden case,
so the failure is checked on every PR from then on.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ticketpilot.cache import AnswerCache
from ticketpilot.checkpoint import load
from ticketpilot.store import Store, utcnow


def record_feedback(store: Store, run_id: str, rating: str, comment: str | None,
                    candidates_dir: Path) -> dict[str, Any]:
    if rating not in {"up", "down"}:
        raise ValueError("rating must be 'up' or 'down'")
    state = load(store, run_id)
    if state is None:
        raise KeyError(run_id)
    store.execute("INSERT INTO feedback(id,run_id,rating,comment,created_at) VALUES(?,?,?,?,?)",
                  (f"fb_{uuid.uuid4().hex[:10]}", run_id, rating, comment, utcnow()))
    result: dict[str, Any] = {"run_id": run_id, "rating": rating}
    if rating == "down":
        result["evicted_cache_entries"] = AnswerCache.evict_run(store, run_id)
        path = Path(candidates_dir) / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(build_candidate(store, run_id, comment), indent=2))
        result["eval_candidate"] = str(path)
    return result


def build_candidate(store: Store, run_id: str, comment: str | None) -> dict[str, Any]:
    state = load(store, run_id)
    tools = [c["name"] for c in state.tool_calls]
    draft = next((c["input"].get("body") for c in state.tool_calls if c["name"] == "send_reply"), None)
    t = state.ticket_redacted
    return {
        "id": f"prod-{run_id}",
        "source": "prod-failure",
        "tags": ["needs-labeling"],
        "notes": f"Thumbs-down from review: {comment or '(no comment)'}",
        "ticket": {
            "id": t["id"],
            "from_email": "TODO: map to a fixture customer (tickets are stored redacted)",
            "subject": t["subject"],
            "body": t["body"],
        },
        "observed": {
            "status": state.status, "route": state.route.get("route"), "model": state.model,
            "prompt": state.prompt_ref, "tools": tools, "draft_reply": draft,
            "approvals": store.query("SELECT tool, status, decision_note FROM approvals WHERE run_id=?", (run_id,)),
            "summary": state.summary,
        },
        "expect": {
            "status": ["TODO"],
            "required_tools": tools,
            "forbidden_tools": [],
            "reply_must_include": [],
            "reply_must_not_include": [],
        },
    }
