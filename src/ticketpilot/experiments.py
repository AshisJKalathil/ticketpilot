"""A/B test new models on a slice of real traffic first.

Experiments live in ``config/experiments.yaml``. Assignment is a pure hash of
``experiment name + ticket id``: deterministic (a retried or resumed ticket
stays in its arm), uniform, and needs no shared state. Only tickets on the
experiment's route are eligible, and only ``traffic`` of those get the
treatment - everything else keeps running on the control model.

``scripts/ab_report.py`` compares arms on resolution rate, human-approval
rate, thumbs-down rate, cost per task and latency.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Experiment:
    name: str
    route: str
    traffic: float
    control_model: str
    treatment_model: str
    status: str = "running"


@dataclass(frozen=True)
class Assignment:
    experiment: str
    arm: str  # control | treatment
    model: str

    @property
    def label(self) -> str:
        return f"{self.experiment}:{self.arm}"


def bucket(experiment: str, unit_id: str) -> float:
    digest = hashlib.sha256(f"{experiment}:{unit_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def load_experiments(path: Path | None) -> list[Experiment]:
    if not path or not Path(path).exists():
        return []
    data = yaml.safe_load(Path(path).read_text()) or {}
    out = []
    for e in data.get("experiments", []):
        out.append(Experiment(
            name=e["name"], route=e["route"], traffic=float(e["traffic"]),
            control_model=e["control"]["model"], treatment_model=e["treatment"]["model"],
            status=e.get("status", "running"),
        ))
    return out


def assign(experiments: list[Experiment], route: str, unit_id: str) -> Assignment | None:
    for exp in experiments:
        if exp.status != "running" or exp.route != route:
            continue
        treated = bucket(exp.name, unit_id) < exp.traffic
        return Assignment(exp.name, "treatment" if treated else "control",
                          exp.treatment_model if treated else exp.control_model)
    return None
