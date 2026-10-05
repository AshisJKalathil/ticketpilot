"""Eval runner + CI gate.

    python -m evals.run                         # mock provider, all cases
    python -m evals.run --provider anthropic    # real Claude (costs money)
    python -m evals.run --gate evals/baseline.mock.json   # exit 1 on regression
    python -m evals.run --tags security --case injection-in-ticket

Every case runs against a freshly seeded database, so cases are independent
and can run concurrently.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evals.scorers import score_case
from ticketpilot.config import REPO_ROOT, Settings
from ticketpilot.data.seed import seed
from ticketpilot.harness import run_ticket_to_completion
from ticketpilot.prompts import PromptRegistry
from ticketpilot.schemas import Ticket
from ticketpilot.store import Store

GOLDEN = REPO_ROOT / "evals" / "golden.jsonl"


def load_cases(path: Path = GOLDEN, tags: list[str] | None = None, ids: list[str] | None = None) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if tags:
        cases = [c for c in cases if set(tags) & set(c["tags"])]
    if ids:
        cases = [c for c in cases if c["id"] in ids]
    return cases


def run_case(case: dict, base: Settings, workdir: Path, judge=None) -> dict:
    db = workdir / f"{case['id']}.db"
    overrides = {"db_path": db, "fault_injection": case.get("fault_injection", {})}
    overrides.update(case.get("budget", {}))
    settings = base.with_overrides(**overrides)
    store = Store(db)
    seed(store)
    started = time.perf_counter()
    try:
        outcome = run_ticket_to_completion(
            Ticket(**case["ticket"]), settings=settings, store=store, approvals=case.get("approvals")
        )
    except Exception as exc:  # an eval must never crash the suite
        return {"id": case["id"], "passed": False, "error": f"{type(exc).__name__}: {exc}", "scores": [],
                "cost_usd": 0.0, "latency_s": time.perf_counter() - started, "steps": 0, "route": None}
    latency = time.perf_counter() - started
    scores = score_case(case, outcome)
    if judge and outcome.final_reply:
        scores.append(judge(case, outcome))
    passed = all(s.passed for s in scores if s.hard)
    return {
        "id": case["id"], "tags": case["tags"], "passed": passed, "status": outcome.status,
        "route": outcome.route, "model": outcome.model, "steps": outcome.steps,
        "cost_usd": round(outcome.cost_usd, 6), "latency_s": round(latency, 3),
        "tools": [c["name"] for c in outcome.tool_calls], "run_id": outcome.run_id,
        "scores": [s.as_dict() for s in scores],
    }


def summarize(results: list[dict], settings: Settings) -> dict:
    n = len(results)
    passed = [r for r in results if r["passed"]]
    per_scorer: dict[str, list[bool]] = {}
    for r in results:
        for s in r["scores"]:
            per_scorer.setdefault(s["name"], []).append(s["passed"])
    latencies = sorted(r["latency_s"] for r in results)
    total_cost = sum(r["cost_usd"] for r in results)
    return {
        "provider": settings.provider,
        "models": {"small": settings.small_model, "reasoning": settings.reasoning_model},
        "prompt_versions": PromptRegistry(settings.prompts_dir).versions(),
        "cases": n,
        "passed": len(passed),
        "pass_rate": round(len(passed) / n, 4) if n else 0.0,
        "scorer_pass_rates": {k: round(sum(v) / len(v), 4) for k, v in sorted(per_scorer.items())},
        "cost_total_usd": round(total_cost, 6),
        "cost_per_task_usd": round(total_cost / n, 6) if n else 0.0,
        "cost_per_successful_task_usd": round(total_cost / len(passed), 6) if passed else None,
        "latency_p50_s": round(statistics.median(latencies), 3) if latencies else 0.0,
        "latency_p95_s": round(latencies[max(0, int(0.95 * n) - 1)], 3) if latencies else 0.0,
        "avg_steps": round(statistics.mean(r["steps"] for r in results), 2) if results else 0.0,
        "routes": dict(Counter(r["route"] for r in results)),
    }


def check_gate(summary: dict, baseline: dict) -> list[str]:
    failures = []
    if summary["pass_rate"] < baseline.get("min_pass_rate", 0):
        failures.append(f"pass_rate {summary['pass_rate']} < {baseline['min_pass_rate']}")
    for scorer, floor in baseline.get("min_scorer_pass_rates", {}).items():
        got = summary["scorer_pass_rates"].get(scorer)
        if got is not None and got < floor:
            failures.append(f"{scorer} pass rate {got} < {floor}")
    cap = baseline.get("max_cost_per_task_usd")
    if cap is not None and summary["cost_per_task_usd"] > cap:
        failures.append(f"cost_per_task ${summary['cost_per_task_usd']} > ${cap}")
    cap = baseline.get("max_latency_p95_s")
    if cap is not None and summary["latency_p95_s"] > cap:
        failures.append(f"latency_p95 {summary['latency_p95_s']}s > {cap}s")
    return failures


def to_markdown(summary: dict, results: list[dict], failures: list[str]) -> str:
    lines = [
        f"## Eval results - {summary['provider']}",
        "",
        f"**{summary['passed']}/{summary['cases']} passed** ({summary['pass_rate']:.0%}) · "
        f"cost/task ${summary['cost_per_task_usd']:.4f} · p50 {summary['latency_p50_s']}s · "
        f"avg steps {summary['avg_steps']}",
        "",
        "| scorer | pass rate |", "|---|---|",
        *[f"| {k} | {v:.0%} |" for k, v in summary["scorer_pass_rates"].items()],
        "",
        "| case | result | route | steps | cost | failing checks |", "|---|---|---|---|---|---|",
    ]
    for r in results:
        failing = ", ".join(f"{s['name']}: {s['detail']}" for s in r["scores"] if not s["passed"]) or r.get("error", "")
        lines.append(f"| {r['id']} | {'PASS' if r['passed'] else 'FAIL'} | {r.get('route')} | {r['steps']} | "
                     f"${r['cost_usd']:.4f} | {failing} |")
    if failures:
        lines += ["", "### Gate FAILED", *[f"- {f}" for f in failures]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["mock", "anthropic"], default=None)
    ap.add_argument("--tags", nargs="*")
    ap.add_argument("--case", nargs="*", dest="ids")
    ap.add_argument("--dataset", type=Path, default=GOLDEN)
    ap.add_argument("--gate", type=Path, help="baseline thresholds JSON; exit 1 if any is violated")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "evals" / "reports" / "latest.json")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--judge", action="store_true", help="add an LLM-as-judge reply quality score (live only)")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    if args.provider:
        settings = settings.with_overrides(provider=args.provider)
    cases = load_cases(args.dataset, args.tags, args.ids)
    judge = None
    if args.judge:
        from evals.judge import make_judge

        judge = make_judge(settings)

    with tempfile.TemporaryDirectory(prefix="tp-evals-") as tmp, \
            ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = list(pool.map(lambda c: run_case(c, settings, Path(tmp), judge), cases))

    summary = summarize(results, settings)
    failures = check_gate(summary, json.loads(args.gate.read_text())) if args.gate else []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "results": results, "gate_failures": failures}, indent=2))

    md = to_markdown(summary, results, failures)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(md + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
