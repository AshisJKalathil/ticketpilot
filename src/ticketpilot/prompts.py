"""Prompts are versioned like code.

Each prompt lives in ``prompts/<id>.yaml`` with a semver ``version`` and a
changelog. ``prompts/prompts.lock.json`` pins the content hash of every
version, and a unit test fails if a template changes without a version bump -
so every trace and eval report can name the exact prompt that produced it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    description: str
    template: str
    sha256: str

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}+{self.sha256[:8]}"

    @property
    def variables(self) -> set[str]:
        return set(_VAR.findall(self.template))

    def render(self, **values: object) -> str:
        missing = self.variables - values.keys()
        if missing:
            raise KeyError(f"prompt {self.id} missing variables: {sorted(missing)}")
        return _VAR.sub(lambda m: str(values[m.group(1)]), self.template)


class PromptRegistry:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.lock_path = self.directory / "prompts.lock.json"

    @cache  # noqa: B019 - registries are few and long-lived
    def get(self, prompt_id: str) -> Prompt:
        data = yaml.safe_load((self.directory / f"{prompt_id}.yaml").read_text())
        if data.get("id") != prompt_id:
            raise ValueError(f"{prompt_id}.yaml declares id={data.get('id')!r}")
        if not _SEMVER.match(str(data.get("version", ""))):
            raise ValueError(f"{prompt_id}.yaml needs a semver version, got {data.get('version')!r}")
        template = data["template"]
        return Prompt(
            id=prompt_id,
            version=str(data["version"]),
            description=data.get("description", ""),
            template=template,
            sha256=hashlib.sha256(template.encode()).hexdigest(),
        )

    def all(self) -> list[Prompt]:
        return [self.get(p.stem) for p in sorted(self.directory.glob("*.yaml"))]

    def versions(self) -> dict[str, str]:
        return {p.id: p.ref for p in self.all()}

    def lock_problems(self) -> list[str]:
        """Return human-readable problems; empty means every prompt matches its lock entry."""
        lock = json.loads(self.lock_path.read_text()) if self.lock_path.exists() else {}
        problems = []
        for p in self.all():
            entry = lock.get(p.id)
            if entry is None:
                problems.append(f"{p.id}: not in prompts.lock.json - run scripts/lock_prompts.py")
            elif entry["sha256"] != p.sha256 and entry["version"] == p.version:
                problems.append(f"{p.id}: template changed but version is still {p.version} - bump it")
            elif entry["sha256"] != p.sha256 or entry["version"] != p.version:
                problems.append(f"{p.id}: lock is stale ({entry['version']} -> {p.version}) - run scripts/lock_prompts.py")
        return problems

    def write_lock(self) -> dict:
        lock = json.loads(self.lock_path.read_text()) if self.lock_path.exists() else {}
        for p in self.all():
            old = lock.get(p.id)
            if old and old["sha256"] != p.sha256 and old["version"] == p.version:
                raise SystemExit(f"{p.id}: template changed without a version bump ({p.version})")
            lock[p.id] = {"version": p.version, "sha256": p.sha256}
        self.lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
        return lock
