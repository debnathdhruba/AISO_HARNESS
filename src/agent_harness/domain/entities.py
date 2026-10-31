"""Minimal durable entities. JSON is immutable text, never live runtime objects."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from agent_harness.domain.errors import require
from agent_harness.domain.states import ControlIntent, RunStatus, TaskStatus


def identifier(value: str) -> None:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        require(False, "Expected a UUID identifier")
        return
    require(str(parsed) == value, "Expected a canonical UUID identifier")


def utc(value: datetime) -> None:
    require(
        value.tzinfo is not None and value.utcoffset() == timedelta(0),
        "Expected an aware UTC timestamp",
    )


@dataclass(frozen=True)
class JsonObject:
    """Validated canonical JSON object; callers cannot mutate a persisted snapshot."""

    text: str = "{}"

    def __post_init__(self) -> None:
        try:
            value = json.loads(self.text)
            require(isinstance(value, dict), "Expected a JSON object")
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            require(False, "Expected a finite JSON object")
            return
        object.__setattr__(self, "text", canonical)


@dataclass(frozen=True)
class RunConfig:
    agent_definition_id: str
    agent_version: str
    model: JsonObject = field(default_factory=JsonObject)
    policy: JsonObject = field(default_factory=JsonObject)
    budgets: JsonObject = field(default_factory=JsonObject)
    memory: JsonObject = field(default_factory=JsonObject)
    workspace: JsonObject = field(default_factory=JsonObject)

    def __post_init__(self) -> None:
        require(
            bool(self.agent_definition_id.strip() and self.agent_version.strip()),
            "Agent definition and version are required",
        )


@dataclass(frozen=True)
class Run:
    run_id: str
    user_id: str
    task_input: str
    config: RunConfig
    created_at: datetime
    updated_at: datetime
    status: RunStatus = RunStatus.CREATED
    version: int = 0
    deadline: datetime | None = None
    intent: ControlIntent = ControlIntent.NONE
    result: JsonObject | None = None
    error: JsonObject | None = None

    def __post_init__(self) -> None:
        identifier(self.run_id)
        require(
            bool(self.user_id.strip() and self.task_input.strip()), "User and task are required"
        )
        require(self.version >= 0, "Version cannot be negative")
        utc(self.created_at)
        utc(self.updated_at)
        require(self.updated_at >= self.created_at, "Update precedes creation")
        if self.deadline is not None:
            utc(self.deadline)


@dataclass(frozen=True)
class Task:
    task_id: str
    run_id: str
    user_id: str
    objective: str
    created_at: datetime
    updated_at: datetime
    acceptance_criteria: JsonObject = field(default_factory=JsonObject)
    parent_task_id: str | None = None
    dependencies: tuple[str, ...] = ()
    agent_instance_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    version: int = 0
    deadline: datetime | None = None
    result: JsonObject | None = None

    def __post_init__(self) -> None:
        identifier(self.task_id)
        identifier(self.run_id)
        require(
            bool(self.user_id.strip() and self.objective.strip()), "User and objective required"
        )
        require(self.version >= 0, "Version cannot be negative")
        utc(self.created_at)
        utc(self.updated_at)
        require(self.updated_at >= self.created_at, "Update precedes creation")
        if self.deadline is not None:
            utc(self.deadline)
        for value in (self.parent_task_id, self.agent_instance_id, *self.dependencies):
            if value is not None:
                identifier(value)
        require(
            self.task_id not in self.dependencies and self.task_id != self.parent_task_id,
            "Task cannot depend on or parent itself",
        )
        require(len(set(self.dependencies)) == len(self.dependencies), "Duplicate dependencies")


@dataclass(frozen=True)
class Event:
    event_id: str
    run_id: str
    user_id: str
    sequence: int
    event_type: str
    timestamp: datetime
    payload: JsonObject
    task_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        identifier(self.event_id)
        identifier(self.run_id)
        if self.task_id is not None:
            identifier(self.task_id)
        utc(self.timestamp)
        require(
            self.sequence > 0 and self.schema_version == 1, "Unsupported event version/sequence"
        )
        require(bool(self.user_id.strip() and self.event_type.strip()), "Event scope/type required")


@dataclass(frozen=True)
class Checkpoint:
    task_id: str
    run_id: str
    user_id: str
    state_version: int
    last_event_sequence: int
    continuation: JsonObject
    version: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        identifier(self.task_id)
        identifier(self.run_id)
        require(bool(self.user_id.strip()), "Checkpoint user required")
        require(self.schema_version == 1, "Unsupported checkpoint schema version")
        require(
            self.state_version >= 0 and self.version >= 0 and self.last_event_sequence > 0,
            "Invalid checkpoint version/sequence",
        )
