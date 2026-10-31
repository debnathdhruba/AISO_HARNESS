from dataclasses import FrozenInstanceError, replace
from datetime import datetime

import pytest

from agent_harness.domain.entities import Checkpoint, JsonObject, Run, Task
from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.states import (
    INVOCATION_TRANSITIONS,
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    InvocationStatus,
    RunStatus,
    TaskStatus,
    validate_transition,
)


@pytest.mark.parametrize(
    "enum,table",
    [
        (RunStatus, RUN_TRANSITIONS),
        (TaskStatus, TASK_TRANSITIONS),
        (InvocationStatus, INVOCATION_TRANSITIONS),
    ],
)
def test_complete_transition_tables(enum, table) -> None:
    assert set(table) == set(enum)
    for current in enum:
        for target in enum:
            if target in table[current]:
                validate_transition(current, target)
            else:
                with pytest.raises(HarnessError) as error:
                    validate_transition(current, target)
                assert error.value.code == ErrorCode.VALIDATION


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.COMPLETED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
        InvocationStatus.CANCELLED,
    ],
)
def test_terminal_statuses_cannot_reopen(status) -> None:
    for candidate in type(status):
        with pytest.raises(HarnessError):
            validate_transition(status, candidate)


def test_reconciliation_does_not_reexecute_unknown_write() -> None:
    validate_transition(InvocationStatus.UNKNOWN, InvocationStatus.SUCCEEDED)
    validate_transition(InvocationStatus.UNKNOWN, InvocationStatus.FAILED)
    with pytest.raises(HarnessError):
        validate_transition(InvocationStatus.UNKNOWN, InvocationStatus.EXECUTING)


def test_run_and_task_validations(run: Run, task: Task) -> None:
    for changes in (
        {"run_id": "name"},
        {"user_id": ""},
        {"version": -1},
        {"deadline": datetime(2026, 1, 1)},
    ):
        with pytest.raises(HarnessError):
            replace(run, **changes)
    for changes in (
        {"parent_task_id": task.task_id},
        {"dependencies": (task.task_id,)},
        {"dependencies": (run.run_id, run.run_id)},
    ):
        with pytest.raises(HarnessError):
            replace(task, **changes)
    with pytest.raises(FrozenInstanceError):
        run.user_id = "bob"


@pytest.mark.parametrize("text", ["[]", "null", "{", '{"x":NaN}', '{"x":Infinity}'])
def test_json_rejects_non_object_or_non_finite_values(text: str) -> None:
    with pytest.raises(HarnessError):
        JsonObject(text)


def test_checkpoint_rejects_future_schema(task: Task) -> None:
    with pytest.raises(HarnessError, match="Unsupported checkpoint"):
        Checkpoint(task.task_id, task.run_id, task.user_id, 0, 1, JsonObject(), schema_version=2)


def test_error_contract() -> None:
    error = HarnessError(
        ErrorCode.TRANSIENT_PROVIDER,
        "Provider unavailable",
        retryable=True,
        diagnostic_reference="local-diagnostic-id",
    )
    assert error.code == "transient_provider"
    assert error.retryable
    assert error.diagnostic_reference == "local-diagnostic-id"
    assert str(error) == "Provider unavailable"
