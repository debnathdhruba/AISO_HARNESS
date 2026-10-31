from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio

from agent_harness.domain.entities import Run, RunConfig, Task
from agent_harness.persistence.sqlite import SQLiteStore


class FixedClock:
    def utc_now(self) -> datetime:
        return datetime(2026, 10, 31, tzinfo=UTC)

    def monotonic(self) -> float:
        return 100.0


class SequenceIds:
    def __init__(self) -> None:
        self.ordinal = 100

    def new_id(self) -> str:
        self.ordinal += 1
        return str(UUID(int=self.ordinal))


@pytest.fixture
def run() -> Run:
    now = FixedClock().utc_now()
    return Run(str(UUID(int=1)), "alice", "Private input", RunConfig("worker", "1"), now, now)


@pytest.fixture
def task(run: Run) -> Task:
    return Task(str(UUID(int=2)), run.run_id, run.user_id, "Work", run.created_at, run.created_at)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    async with SQLiteStore(tmp_path / "state.sqlite", FixedClock(), SequenceIds()) as instance:
        yield instance
