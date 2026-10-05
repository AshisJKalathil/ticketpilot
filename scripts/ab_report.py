"""Compare experiment arms on live traffic.

    python scripts/ab_report.py --db var/ticketpilot.db

Resolution rate is compared with a two-proportion z-test; cost per resolved
task and latency are reported per arm. Widen a treatment's traffic slice only
when quality holds *and* cost per resolved task is acceptable.
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict

from ticketpilot.metrics import _summarise
from ticketpilot.store import Store


def two_proportion_z(s1: int, n1: int, s2: int, n2: int) -> tuple[float, float]:
    if min(n1, n2) == 0:
        return 0.0, 1.0
    p = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-9
    z = (s2 / n2 - s1 / n1) / se
    return z, math.erfc(abs(z) / math.sqrt(2))  # two-sided p-value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="var/ticketpilot.db")
    ap.add_argument("--min-samples", type=int, default=200)
    args = ap.parse_args()
    store = Store(args.db)
    rows = store.query("SELECT status, arm, cost_usd, steps, active_seconds FROM runs WHERE arm IS NOT NULL")
    experiments: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        name, arm = r["arm"].rsplit(":", 1)
        experiments[name][arm].append(r)
    if not experiments:
        print("no experiment traffic yet")
        return 0
    for name, arms in experiments.items():
        print(f"\n== {name}")
        stats = {arm: _summarise(rs) for arm, rs in arms.items()}
        for arm, st in stats.items():
            print(f"  {arm:10s} n={st['finished']:4d} resolved={st['resolution_rate']} "
                  f"cost/resolved=${st['cost_per_resolved_task_usd']} p50={st['p50_active_seconds']}s")
        c, t = stats.get("control"), stats.get("treatment")
        if c and t:
            z, p = two_proportion_z(c["resolved"], c["finished"], t["resolved"], t["finished"])
            print(f"  resolution delta z={z:+.2f} p={p:.3f}")
            if min(c["finished"], t["finished"]) < args.min_samples:
                print(f"  verdict: keep collecting (need >= {args.min_samples} finished runs per arm)")
            elif p < 0.05 and z > 0:
                print("  verdict: treatment resolves more tickets - check cost/resolved before widening traffic")
            elif p < 0.05:
                print("  verdict: treatment is worse - stop the experiment")
            else:
                print("  verdict: no significant difference - prefer the cheaper arm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
