"""Export your best production trajectories as a fine-tuning / distillation set.

"Best" is deliberately strict - a training example must have:

* finished ``completed`` (not escalated, not over budget);
* every human approval granted **without edits** (an edited reply means the
  agent's version was not good enough to imitate);
* no thumbs-down, and either a thumbs-up or (with --include-unrated) no rating;
* no injection flags in the run (never train on attacker-shaped inputs);
* cost and step count at or below the median for its route (efficient paths only).

Everything exported is the *redacted* conversation the model actually saw -
placeholders, never raw PII. Output is provider-neutral chat JSONL
(``{"messages": [...], "tools": [...], "metadata": {...}}``), split
deterministically into train/validation by hashing the run id.

Use it to fine-tune a small model on your provider's fine-tuning offering,
or as a distillation set to make the cheap route as good as the expensive one.

    python -m finetune.export --db var/ticketpilot.db --out finetune/out
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path

from ticketpilot.checkpoint import RunState, load
from ticketpilot.store import Store
from ticketpilot.tools.registry import ToolRegistry


def eligible_runs(store: Store, include_unrated: bool) -> list[RunState]:
    rows = store.query("""
        SELECT r.id, r.route, r.cost_usd, r.steps,
               (SELECT COUNT(*) FROM approvals a WHERE a.run_id = r.id
                 AND (a.status != 'approved' OR a.edited_args_json IS NOT NULL)) AS bad_approvals,
               (SELECT COUNT(*) FROM feedback f WHERE f.run_id = r.id AND f.rating = 'down') AS downs,
               (SELECT COUNT(*) FROM feedback f WHERE f.run_id = r.id AND f.rating = 'up') AS ups
        FROM runs r WHERE r.status = 'completed'
    """)
    rows = [r for r in rows if r["bad_approvals"] == 0 and r["downs"] == 0 and (r["ups"] or include_unrated)]
    by_route: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_route[r["route"]].append(r)
    keep = []
    for group in by_route.values():
        cost_med = statistics.median(r["cost_usd"] for r in group)
        step_med = statistics.median(r["steps"] for r in group)
        keep += [r["id"] for r in group if r["cost_usd"] <= cost_med and r["steps"] <= step_med]
    states = [load(store, rid) for rid in keep]
    return [s for s in states if s and not s.guard_flags.get("injection") and not s.answer_cache]


MODEL_SPECIFIC_BLOCKS = {"thinking", "redacted_thinking", "fallback"}


def _portable(message: dict) -> dict:
    """Drop blocks that only make sense to the model that produced them."""
    if message["role"] != "assistant" or not isinstance(message["content"], list):
        return message
    return {"role": "assistant", "content": [b for b in message["content"] if b.get("type") not in MODEL_SPECIFIC_BLOCKS]}


def to_example(state: RunState, tools: list[dict]) -> dict:
    return {
        "messages": [{"role": "system", "content": state.system}, *map(_portable, state.messages)],
        "tools": tools,
        "metadata": {"run_id": state.run_id, "route": state.route.get("route"), "model": state.model,
                     "prompt": state.prompt_ref},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="var/ticketpilot.db")
    ap.add_argument("--out", type=Path, default=Path("finetune/out"))
    ap.add_argument("--val-fraction", type=float, default=0.1)
    ap.add_argument("--include-unrated", action="store_true")
    args = ap.parse_args(argv)

    store = Store(args.db)
    registry = ToolRegistry()
    seen: set[str] = set()
    splits: dict[str, list[dict]] = {"train": [], "validation": []}
    for state in eligible_runs(store, args.include_unrated):
        fingerprint = hashlib.sha256(json.dumps(state.ticket_redacted, sort_keys=True).encode()).hexdigest()
        if fingerprint in seen:  # de-duplicate identical tickets
            continue
        seen.add(fingerprint)
        bucket = int(hashlib.sha256(state.run_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        split = "validation" if bucket < args.val_fraction else "train"
        splits[split].append(to_example(state, registry.definitions(set(state.scopes))))

    args.out.mkdir(parents=True, exist_ok=True)
    for name, rows in splits.items():
        with open(args.out / f"{name}.jsonl", "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
    print(json.dumps({k: len(v) for k, v in splits.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
