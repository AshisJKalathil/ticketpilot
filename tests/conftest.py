import json
from pathlib import Path

import pytest

from ticketpilot.config import REPO_ROOT, Settings
from ticketpilot.data.seed import seed
from ticketpilot.llm import mock
from ticketpilot.retry import reset_breakers
from ticketpilot.schemas import Ticket
from ticketpilot.store import Store

TICKETS = {json.loads(line)["id"]: json.loads(line)
           for line in (REPO_ROOT / "src/ticketpilot/data/tickets.jsonl").read_text().splitlines() if line}


@pytest.fixture(autouse=True)
def _isolation():
    reset_breakers()
    mock._seen_prefixes.clear()
    yield
    reset_breakers()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    seed(s)
    return s


@pytest.fixture
def settings(store: Store, tmp_path: Path) -> Settings:
    return Settings(provider="mock", db_path=store.path, experiments_enabled=False,
                    eval_candidates_dir=tmp_path / "candidates")


@pytest.fixture
def ticket():
    return lambda tid, **over: Ticket(**{**TICKETS[tid], **over})
