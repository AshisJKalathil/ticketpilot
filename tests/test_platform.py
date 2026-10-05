"""Prompt versioning, routing, experiments, eval gate, fine-tune export."""

import json
import shutil

from evals.run import check_gate
from finetune.export import main as export_main
from ticketpilot.config import REPO_ROOT, Settings
from ticketpilot.experiments import Experiment, assign, bucket
from ticketpilot.harness import run_ticket_to_completion
from ticketpilot.intents import analyze
from ticketpilot.llm.router import Router
from ticketpilot.prompts import PromptRegistry


def test_every_prompt_matches_its_lock():
    assert PromptRegistry(REPO_ROOT / "prompts").lock_problems() == []


def test_editing_a_prompt_without_a_version_bump_is_caught(tmp_path):
    shutil.copytree(REPO_ROOT / "prompts", tmp_path / "prompts")
    path = tmp_path / "prompts" / "agent_system.yaml"
    path.write_text(path.read_text().replace("Acme Trail Co.", "Acme Trail Company", 1))
    problems = PromptRegistry(tmp_path / "prompts").lock_problems()
    assert any("bump" in p for p in problems)


def test_router_sends_routine_tickets_to_small_model_and_risky_ones_to_reasoning_model():
    router = Router(Settings(provider="mock"), PromptRegistry(REPO_ROOT / "prompts"))
    easy = router.decide(analyze("Where is my order A1002?"), text="", tier="standard", injection_flagged=False)
    hard = router.decide(analyze("Tent A1004 arrived torn, I'm disappointed, trip next weekend"), text="",
                         tier="vip", injection_flagged=False)
    legal = router.decide(analyze("I'm filing a chargeback, my lawyer will call"), text="", tier=None,
                          injection_flagged=False)
    assert (easy.route, easy.model) == ("easy", "claude-haiku-4-5")
    assert (hard.route, hard.model, hard.effort) == ("hard", "claude-opus-5-5", "high")
    assert legal.route == "hard"


def test_experiment_assignment_is_deterministic_and_respects_traffic():
    exp = [Experiment("e1", "easy", 0.10, "claude-haiku-4-5", "claude-sonnet-5-5")]
    assert assign(exp, "easy", "T-1") == assign(exp, "easy", "T-1")
    assert assign(exp, "hard", "T-1") is None
    treated = sum(assign(exp, "easy", f"T-{i}").arm == "treatment" for i in range(10_000))
    assert 850 < treated < 1150
    assert 0 <= bucket("e1", "x") < 1


def test_eval_gate_blocks_regressions():
    baseline = {"min_pass_rate": 1.0, "min_scorer_pass_rates": {"no_pii_to_model": 1.0},
                "max_cost_per_task_usd": 0.05}
    good = {"pass_rate": 1.0, "scorer_pass_rates": {"no_pii_to_model": 1.0}, "cost_per_task_usd": 0.01,
            "latency_p95_s": 1}
    assert check_gate(good, baseline) == []
    bad = {**good, "pass_rate": 0.95, "scorer_pass_rates": {"no_pii_to_model": 0.9}, "cost_per_task_usd": 0.2}
    assert len(check_gate(bad, baseline)) == 3


def test_finetune_export_keeps_only_clean_unedited_runs(store, settings, ticket, tmp_path):
    for tid in ("T-2001", "T-2002", "T-2012", "T-2013"):
        out = run_ticket_to_completion(ticket(tid), settings=settings, store=store)
        store.execute("INSERT INTO feedback(id, run_id, rating, created_at) VALUES(?,?,?,?)",
                      (f"f{tid}", out.run_id, "up", "now"))
    export_main(["--db", str(store.path), "--out", str(tmp_path / "ft"), "--val-fraction", "0"])
    rows = [json.loads(line) for line in (tmp_path / "ft" / "train.jsonl").read_text().splitlines()]
    assert rows, "expected at least one exported example"
    blob = json.dumps(rows)
    assert "@example." not in blob and "Summit#2026" not in blob  # redacted conversations only
    assert all(r["messages"][0]["role"] == "system" for r in rows)
