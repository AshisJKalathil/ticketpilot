"""Promote a labelled eval candidate (from a thumbs-down) into the golden set.

    python scripts/promote_candidate.py evals/candidates/run_abc.json \
        --from-email daniel.okafor@example.com

Candidates are stored redacted, so the ticket must be mapped onto a fixture
customer. The script refuses to promote while any field still says TODO, so
an unlabelled failure can never silently enter the gate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

GOLDEN = Path(__file__).resolve().parents[1] / "evals" / "golden.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidate", type=Path)
    ap.add_argument("--from-email", required=True, help="fixture customer to attribute the ticket to")
    ap.add_argument("--id", help="golden case id (default: candidate id)")
    args = ap.parse_args()

    case = json.loads(args.candidate.read_text())
    case["ticket"]["from_email"] = args.from_email
    if args.id:
        case["id"] = args.id
    case.pop("observed", None)
    case["tags"] = [t for t in case.get("tags", []) if t != "needs-labeling"] + ["regression"]
    if "TODO" in json.dumps(case):
        print("refusing to promote: the candidate still contains TODO fields - label the expect block first",
              file=sys.stderr)
        return 1
    existing = {json.loads(line)["id"] for line in GOLDEN.read_text().splitlines() if line.strip()}
    if case["id"] in existing:
        print(f"refusing to promote: case id {case['id']} already exists", file=sys.stderr)
        return 1
    with GOLDEN.open("a") as fh:
        fh.write(json.dumps(case) + "\n")
    done = args.candidate.parent / "promoted"
    done.mkdir(exist_ok=True)
    shutil.move(str(args.candidate), done / args.candidate.name)
    print(f"promoted {case['id']} -> {GOLDEN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
