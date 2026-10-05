"""Runtime settings, read once from the environment.

Everything that changes behaviour between local dev, CI, evals and the public
demo lives here so it can be audited in one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _default_provider() -> str:
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "mock"


@dataclass(frozen=True)
class Settings:
    provider: str = "mock"
    small_model: str = "claude-haiku-4-5"
    reasoning_model: str = "claude-opus-5-5"
    reasoning_effort: str = "high"
    fallback_provider: str = "none"
    aws_region: str = "us-east-1"

    max_cost_usd: float = 0.50
    max_seconds: float = 120.0
    max_steps: int = 12
    max_total_tokens: int = 200_000

    db_path: Path = REPO_ROOT / "var" / "ticketpilot.db"
    store_date: date = date(2026, 10, 6)
    portal_base_url: str = "inproc"
    browser_engine: str = "http"
    browser_channel: str | None = None  # e.g. "chrome" to drive an installed Chrome instead of bundled Chromium
    prompts_dir: Path = REPO_ROOT / "prompts"
    experiments_path: Path = REPO_ROOT / "config" / "experiments.yaml"
    experiments_enabled: bool = True
    mcp_servers_path: Path | None = None
    eval_candidates_dir: Path = REPO_ROOT / "evals" / "candidates"

    public_demo: bool = False
    daily_spend_cap_usd: float = 5.0
    rate_limit_per_min: int = 10

    # Test hooks - never set in production.
    fault_injection: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        mcp_path = os.environ.get("TICKETPILOT_MCP_SERVERS")
        return cls(
            provider=_env("TICKETPILOT_PROVIDER", _default_provider()),
            small_model=_env("TICKETPILOT_SMALL_MODEL", cls.small_model),
            reasoning_model=_env("TICKETPILOT_REASONING_MODEL", cls.reasoning_model),
            reasoning_effort=_env("TICKETPILOT_REASONING_EFFORT", cls.reasoning_effort),
            fallback_provider=_env("TICKETPILOT_FALLBACK_PROVIDER", cls.fallback_provider),
            aws_region=_env("AWS_REGION", cls.aws_region),
            max_cost_usd=float(_env("TICKETPILOT_MAX_COST_USD", str(cls.max_cost_usd))),
            max_seconds=float(_env("TICKETPILOT_MAX_SECONDS", str(cls.max_seconds))),
            max_steps=int(_env("TICKETPILOT_MAX_STEPS", str(cls.max_steps))),
            max_total_tokens=int(_env("TICKETPILOT_MAX_TOTAL_TOKENS", str(cls.max_total_tokens))),
            db_path=Path(_env("TICKETPILOT_DB", str(cls.db_path))),
            store_date=date.fromisoformat(_env("TICKETPILOT_STORE_DATE", cls.store_date.isoformat())),
            portal_base_url=_env("TICKETPILOT_PORTAL_BASE_URL", cls.portal_base_url),
            browser_engine=_env("TICKETPILOT_BROWSER_ENGINE", cls.browser_engine),
            browser_channel=os.environ.get("TICKETPILOT_BROWSER_CHANNEL") or None,
            prompts_dir=Path(_env("TICKETPILOT_PROMPTS_DIR", str(cls.prompts_dir))),
            experiments_path=Path(_env("TICKETPILOT_EXPERIMENTS", str(cls.experiments_path))),
            mcp_servers_path=Path(mcp_path) if mcp_path else None,
            eval_candidates_dir=Path(_env("TICKETPILOT_EVAL_CANDIDATES", str(cls.eval_candidates_dir))),
            public_demo=_env_bool("TICKETPILOT_PUBLIC_DEMO", False),
            daily_spend_cap_usd=float(_env("TICKETPILOT_DAILY_SPEND_CAP_USD", "5.0")),
            rate_limit_per_min=int(_env("TICKETPILOT_RATE_LIMIT_PER_MIN", "10")),
        )

    def with_overrides(self, **kwargs) -> Settings:
        return replace(self, **kwargs)
