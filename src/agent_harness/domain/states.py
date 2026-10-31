"""Explicit allowed edges; terminal work cannot be reopened."""

from enum import StrEnum
from types import MappingProxyType
from typing import TypeVar

from agent_harness.domain.errors import require


class RunStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_CHILDREN = "waiting_children"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class InvocationStatus(StrEnum):
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    READY = "ready"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ControlIntent(StrEnum):
    NONE = "none"
    PAUSE = "pause"
    CANCEL = "cancel"


R = RunStatus
RUN_TRANSITIONS = MappingProxyType(
    {
        R.CREATED: frozenset({R.QUEUED, R.FAILED, R.CANCELLED}),
        R.QUEUED: frozenset({R.RUNNING, R.PAUSED, R.FAILED, R.CANCELLED}),
        R.RUNNING: frozenset({R.WAITING, R.PAUSED, R.COMPLETED, R.PARTIAL, R.FAILED, R.CANCELLED}),
        R.WAITING: frozenset({R.RUNNING, R.PAUSED, R.FAILED, R.CANCELLED}),
        R.PAUSED: frozenset({R.QUEUED, R.FAILED, R.CANCELLED}),
        R.COMPLETED: frozenset(),
        R.PARTIAL: frozenset(),
        R.FAILED: frozenset(),
        R.CANCELLED: frozenset(),
    }
)
T = TaskStatus
TASK_TRANSITIONS = MappingProxyType(
    {
        T.PENDING: frozenset({T.READY, T.PAUSED, T.BLOCKED, T.FAILED, T.CANCELLED}),
        T.READY: frozenset({T.RUNNING, T.PAUSED, T.BLOCKED, T.FAILED, T.CANCELLED}),
        T.RUNNING: frozenset(
            {T.WAITING_CHILDREN, T.WAITING_APPROVAL, T.PAUSED, T.SUCCEEDED, T.FAILED, T.CANCELLED}
        ),
        T.WAITING_CHILDREN: frozenset({T.READY, T.PAUSED, T.BLOCKED, T.FAILED, T.CANCELLED}),
        T.WAITING_APPROVAL: frozenset({T.READY, T.PAUSED, T.FAILED, T.CANCELLED}),
        T.PAUSED: frozenset({T.PENDING, T.READY, T.FAILED, T.CANCELLED}),
        T.SUCCEEDED: frozenset(),
        T.FAILED: frozenset(),
        T.CANCELLED: frozenset(),
        T.BLOCKED: frozenset(),
    }
)
Invocation = InvocationStatus
INVOCATION_TRANSITIONS = MappingProxyType(
    {
        Invocation.PLANNED: frozenset(
            {
                Invocation.AWAITING_APPROVAL,
                Invocation.READY,
                Invocation.FAILED,
                Invocation.CANCELLED,
            }
        ),
        Invocation.AWAITING_APPROVAL: frozenset(
            {Invocation.READY, Invocation.FAILED, Invocation.CANCELLED}
        ),
        Invocation.READY: frozenset(
            {Invocation.EXECUTING, Invocation.FAILED, Invocation.CANCELLED}
        ),
        Invocation.EXECUTING: frozenset(
            {
                Invocation.READY,
                Invocation.SUCCEEDED,
                Invocation.FAILED,
                Invocation.CANCELLED,
                Invocation.UNKNOWN,
            }
        ),
        Invocation.SUCCEEDED: frozenset(),
        Invocation.FAILED: frozenset(),
        Invocation.CANCELLED: frozenset(),
        # Unknown is unresolved: only explicit reconciliation may establish a result.
        Invocation.UNKNOWN: frozenset({Invocation.SUCCEEDED, Invocation.FAILED}),
    }
)

S = TypeVar("S", RunStatus, TaskStatus, InvocationStatus)


def validate_transition(current: S, target: S) -> None:
    require(type(current) is type(target), "Status types must match")
    if isinstance(current, RunStatus):
        require(target in RUN_TRANSITIONS[current], "Invalid run transition")
    elif isinstance(current, TaskStatus):
        require(target in TASK_TRANSITIONS[current], "Invalid task transition")
    else:
        require(target in INVOCATION_TRANSITIONS[current], "Invalid invocation transition")
