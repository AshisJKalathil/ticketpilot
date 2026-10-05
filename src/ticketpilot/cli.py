"""Command line: ``ticketpilot serve | run | resume | trace | metrics | seed | mcp``."""

from __future__ import annotations

import argparse
import json
import sys

from ticketpilot.config import Settings


def _print_event(ev: dict) -> None:
    kind = ev["type"]
    if kind == "text_delta":
        print(ev["text"], end="", flush=True)
    elif kind in {"draft_delta", "tool_input_start"}:
        return
    elif kind == "tool_call":
        print(f"\n→ {ev['name']}({json.dumps(ev['input'])[:160]})")
    elif kind == "tool_result":
        status = "ok" if ev["ok"] else f"ERROR {ev['error']}"
        extra = f" attempts={ev['attempts']}" if ev["attempts"] > 1 else ""
        print(f"  ← {ev['name']}: {status}{extra}")
    elif kind == "approval_required":
        print(f"\n⏸  approval needed [{ev['approval_id']}] {ev['tool']}: {json.dumps(ev['preview'])[:300]}")
    elif kind == "llm_usage":
        print(f"\n  · step {ev['step']} {ev['model']} ${ev['cost_usd']:.4f}")
    elif kind == "run_finished":
        print(f"\n■ {ev['status']}  {json.dumps(ev['metrics'])}")
        if ev.get("final_reply"):
            print("\n" + ev["final_reply"])
    else:
        print(f"\n[{kind}] {json.dumps({k: v for k, v in ev.items() if k != 'type'}, default=str)[:300]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ticketpilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    r = sub.add_parser("run", help="run a demo ticket end to end in the terminal")
    r.add_argument("ticket_id")
    r.add_argument("--reject", nargs="*", default=[], help="tools whose approvals to reject")
    sub.add_parser("resume", help="resume a run from its checkpoint").add_argument("run_id")
    sub.add_parser("trace", help="print a run's trace").add_argument("run_id")
    sub.add_parser("metrics", help="print cost-per-task metrics")
    sub.add_parser("seed", help="(re)seed the demo database").add_argument("--reset", action="store_true")
    sub.add_parser("mcp", help="run the read-only MCP server on stdio")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("ticketpilot.web.app:create_app", factory=True, host=args.host, port=args.port)
        return 0
    if args.cmd == "mcp":
        from ticketpilot.mcp_server import main as mcp_main

        mcp_main([])
        return 0

    from ticketpilot.agent import Agent
    from ticketpilot.checkpoint import load
    from ticketpilot.data.seed import seed
    from ticketpilot.store import Store

    settings = Settings.from_env()
    store = Store(settings.db_path)
    seed(store, reset=getattr(args, "reset", False))
    if args.cmd == "seed":
        print(f"seeded {settings.db_path}")
        return 0
    if args.cmd == "metrics":
        from ticketpilot.metrics import compute_metrics

        print(json.dumps(compute_metrics(store), indent=2))
        return 0
    if args.cmd == "trace":
        from ticketpilot.tracing import load_trace

        for span in load_trace(store, args.run_id):
            print(f"{span['kind']:9s} {span['name']:28s} {span['duration_ms']:8.1f} ms  {span['status']}")
        return 0

    agent = Agent(settings, store)
    if args.cmd == "resume":
        for ev in agent.resume(args.run_id):
            _print_event(ev)
        return 0

    from pathlib import Path

    from ticketpilot.schemas import Ticket

    tickets = {json.loads(line)["id"]: json.loads(line)
               for line in (Path(__file__).parent / "data" / "tickets.jsonl").read_text().splitlines() if line}
    if args.ticket_id not in tickets:
        print(f"unknown ticket; choose one of {', '.join(tickets)}", file=sys.stderr)
        return 2
    events = agent.start(Ticket(**tickets[args.ticket_id]))
    while True:
        for ev in events:
            _print_event(ev)
            run_id = ev.get("run_id", locals().get("run_id"))
        state = load(store, run_id)
        pending = [a for a in agent.approvals.for_run(run_id) if a["status"] == "pending"]
        if state.status != "awaiting_approval" or not pending:
            return 0
        apr = pending[0]
        approve = apr["tool"] not in args.reject
        print(f"\n{'✔ approving' if approve else '✘ rejecting'} {apr['tool']} (as cli-reviewer)")
        events = agent.decide(apr["id"], approved=approve, by="cli-reviewer",
                              note=None if approve else "Rejected from the CLI")


if __name__ == "__main__":
    raise SystemExit(main())
