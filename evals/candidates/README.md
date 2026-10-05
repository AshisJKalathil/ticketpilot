# Eval candidates

Every thumbs-down in the UI writes a JSON file here (`<run_id>.json`) containing the
ticket, the agent's trajectory and the reviewer's comment. They are **not** part of the
gate until a human labels the expected behaviour:

```bash
python scripts/promote_candidate.py evals/candidates/<run_id>.json   # opens $EDITOR on the expect block
```

Promoted cases are appended to `evals/golden.jsonl` with `"source": "prod-failure"`.
