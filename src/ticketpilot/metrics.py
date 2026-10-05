"""Track cost per task, not cost per token.

A cheap model that needs three tries, a human rewrite and an escalation is
not cheap. The headline numbers here are therefore per *ticket*:

* ``cost_per_task``          - all model spend / tickets finished;
* ``cost_per_resolved_task`` - all model spend (including failed and escalated
  runs) / tickets the agent actually resolved. This is the number to optimise;
* plus resolution, escalation, approval-edit and thumbs-down rates, broken
  down by route, model and experiment arm.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from ticketpilot.store import Store, utcnow

TERMINAL = ("completed", "escalated", "budget_exceeded", "failed")


def _summarise(rows: list[dict]) -> dict[str, Any]:
    finished = [r for r in rows if r["status"] in TERMINAL]
    resolved = [r for r in finished if r["status"] == "completed"]
    spend = sum(r["cost_usd"] for r in rows)
    secs = sorted(r["active_seconds"] for r in finished)
    return {
        "runs": len(rows),
        "finished": len(finished),
        "resolved": len(resolved),
        "resolution_rate": round(len(resolved) / len(finished), 3) if finished else None,
        "escalation_rate": round(sum(r["status"] == "escalated" for r in finished) / len(finished), 3)
        if finished else None,
        "spend_usd": round(spend, 4),
        "cost_per_task_usd": round(spend / len(finished), 5) if finished else None,
        "cost_per_resolved_task_usd": round(spend / len(resolved), 5) if resolved else None,
        "p50_active_seconds": round(statistics.median(secs), 2) if secs else None,
        "avg_steps": round(statistics.mean(r["steps"] for r in finished), 2) if finished else None,
    }


def compute_metrics(store: Store) -> dict[str, Any]:
    runs = store.query("SELECT id, status, route, model, arm, cost_usd, steps, active_seconds FROM runs")
    out: dict[str, Any] = {"overall": _summarise(runs)}
    for key in ("route", "model", "arm"):
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in runs:
            if r[key]:
                groups[r[key]].append(r)
        out[f"by_{key}"] = {k: _summarise(v) for k, v in sorted(groups.items())}

    approvals = store.query("SELECT status, edited_args_json IS NOT NULL AS edited FROM approvals")
    decided = [a for a in approvals if a["status"] != "pending"]
    out["approvals"] = {
        "pending": sum(a["status"] == "pending" for a in approvals),
        "approved_unchanged": sum(a["status"] == "approved" and not a["edited"] for a in decided),
        "approved_with_edits": sum(a["status"] == "approved" and a["edited"] for a in decided),
        "rejected": sum(a["status"] == "rejected" for a in decided),
    }
    fb = store.query("SELECT rating, COUNT(*) n FROM feedback GROUP BY rating")
    out["feedback"] = {r["rating"]: r["n"] for r in fb}
    cache = store.one("SELECT COUNT(*) entries, COALESCE(SUM(hits),0) hits FROM semantic_cache")
    out["answer_cache"] = cache
    out["spend_today_usd"] = round(store.spend_today(utcnow()[:10]), 4)
    return out
