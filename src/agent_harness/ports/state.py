"""A scoped transaction is the only write entry point for durable state."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from agent_harness.domain.entities import Checkpoint, Event, JsonObject, Run, Task
from agent_harness.domain.model import ModelResponse, Step, StepStatus
from agent_harness.domain.states import ControlIntent, RunStatus, TaskStatus


class Clock(Protocol):
    def utc_now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class IdGenerator(Protocol):
    def new_id(self) -> str: ...


class StateTransaction(Protocol):
    async def create_step(self, step: Step) -> None: ...
    async def steps(self, run_id: str) -> tuple[Step, ...]: ...
    async def update_step(
        self,
        step_id: str,
        expected: StepStatus,
        status: StepStatus,
        *,
        response: ModelResponse | None = None,
        error: JsonObject | None = None,
    ) -> Step: ...
    async def create_run(self, run: Run) -> None: ...
    async def get_run(self, run_id: str) -> Run | None: ...
    async def transition_run(
        self,
        run_id: str,
        expected_version: int,
        status: RunStatus,
        *,
        result: JsonObject | None = None,
        error: JsonObject | None = None,
    ) -> Run: ...
    async def set_intent(
        self, run_id: str, expected_version: int, intent: ControlIntent
    ) -> Run: ...
    async def create_task(self, task: Task) -> None: ...
    async def get_task(self, task_id: str) -> Task | None: ...
    async def transition_task(
        self,
        task_id: str,
        expected_version: int,
        status: TaskStatus,
        *,
        result: JsonObject | None = None,
    ) -> Task: ...
    async def events(
        self, run_id: str, *, after: int = 0, limit: int = 100
    ) -> tuple[Event, ...]: ...
    async def load_checkpoint(self, task_id: str) -> Checkpoint | None: ...
    async def save_checkpoint(
        self, checkpoint: Checkpoint, expected_version: int | None
    ) -> None: ...


class StateStore(Protocol):
    def transaction(self, user_id: str) -> AbstractAsyncContextManager[StateTransaction]: ...
