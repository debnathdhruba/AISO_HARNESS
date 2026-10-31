"""Serialized units of work. No connection or cursor escapes the transaction owner."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar

import aiosqlite

from agent_harness.domain.entities import Checkpoint, Event, JsonObject, Run, Task
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.model import ModelResponse, Step, StepStatus
from agent_harness.domain.states import (
    RUN_TRANSITIONS,
    ControlIntent,
    InvocationStatus,
    RunStatus,
    TaskStatus,
    validate_transition,
)
from agent_harness.domain.tooling import Approval, ArtifactRecord, Invocation
from agent_harness.models.codec import decode_step, encode_step
from agent_harness.persistence.codec import decode_run, decode_task, encode
from agent_harness.persistence.migrations import migrate
from agent_harness.persistence.tool_codec import (
    decode_artifact,
    decode_invocation,
    encode_artifact,
    encode_invocation,
)
from agent_harness.ports.state import Clock, IdGenerator, StateTransaction


def conflict() -> HarnessError:
    return HarnessError(ErrorCode.CONCURRENCY_CONFLICT, "State version changed", retryable=True)


P = ParamSpec("P")
V = TypeVar("V")


def mutation(
    operation: Callable[Concatenate["SQLiteTransaction", P], Awaitable[V]],
) -> Callable[Concatenate["SQLiteTransaction", P], Coroutine[Any, Any, V]]:
    """A caught write failure must never permit a partial unit of work to commit."""

    async def guarded(tx: "SQLiteTransaction", /, *args: P.args, **kwargs: P.kwargs) -> V:
        tx.check_active()
        try:
            return await operation(tx, *args, **kwargs)
        except sqlite3.Error:
            tx.failed = True
            raise HarnessError(ErrorCode.STORAGE, "Database operation failed") from None
        except BaseException:
            tx.failed = True
            raise

    return guarded


class SQLiteStore:
    def __init__(self, path: Path, clock: Clock, ids: IdGenerator) -> None:
        self._path = path
        self._clock = clock
        self._ids = ids
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None

    async def __aenter__(self) -> "SQLiteStore":
        async with self._lock:
            require(self._connection is None, "Store is already open")
            connection = await aiosqlite.connect(self._path, isolation_level=None)
            try:
                await connection.execute("PRAGMA foreign_keys = ON")
                await connection.execute("PRAGMA journal_mode = WAL")
                await connection.execute("PRAGMA busy_timeout = 5000")
                await migrate(connection)
            except BaseException:
                await connection.close()
                raise
            self._connection = connection
        return self

    async def __aexit__(self, *_: object) -> None:
        require(self._owner is not asyncio.current_task(), "Cannot close inside a transaction")
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    @asynccontextmanager
    async def transaction(self, user_id: str) -> AsyncIterator[StateTransaction]:
        require(bool(user_id.strip()), "Transaction principal is required")
        require(self._owner is not asyncio.current_task(), "Nested transactions are not supported")
        async with self._lock:
            connection = self._connection
            require(connection is not None, "Store is not open")
            assert connection is not None
            self._owner = asyncio.current_task()
            tx = SQLiteTransaction(connection, user_id, self._clock, self._ids)
            try:
                await connection.execute("BEGIN IMMEDIATE")
                yield tx
                tx.check_active()
                if tx.failed:
                    raise HarnessError(
                        ErrorCode.STORAGE, "Transaction must roll back after a failure"
                    )
                await connection.commit()
            except sqlite3.Error:
                await connection.rollback()
                raise HarnessError(ErrorCode.STORAGE, "Database operation failed") from None
            except BaseException:
                await connection.rollback()
                raise
            finally:
                tx.active = False
                self._owner = None


class SQLiteTransaction:
    def __init__(
        self, connection: aiosqlite.Connection, user_id: str, clock: Clock, ids: IdGenerator
    ) -> None:
        self._db = connection
        self._user = user_id
        self._clock = clock
        self._ids = ids
        self._owner = asyncio.current_task()
        self.active = True
        self.failed = False

    def check_active(self) -> None:
        require(
            self.active and self._owner is asyncio.current_task(),
            "Transaction is closed or belongs to another coroutine",
        )

    def _scope(self, user_id: str) -> None:
        self.check_active()
        if user_id != self._user:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Principal does not own this object")

    async def invocations(self, run_id: str) -> tuple[Invocation, ...]:
        self.check_active()
        async with self._db.execute(
            "SELECT snapshot FROM invocations WHERE run_id=? AND user_id=? ORDER BY rowid",
            (run_id, self._user),
        ) as cursor:
            return tuple(decode_invocation(str(row[0])) for row in await cursor.fetchall())

    async def get_invocation(self, invocation_id: str) -> Invocation | None:
        self.check_active()
        async with self._db.execute(
            "SELECT snapshot FROM invocations WHERE invocation_id=? AND user_id=?",
            (invocation_id, self._user),
        ) as cursor:
            row = await cursor.fetchone()
        return decode_invocation(str(row[0])) if row else None

    @mutation
    async def save_invocation(
        self, invocation: Invocation, expected: InvocationStatus | None
    ) -> None:
        self._scope(invocation.user_id)
        previous = await self.get_invocation(invocation.invocation_id)
        if (previous.status if previous else None) != expected:
            raise conflict()
        if previous is None:
            require(invocation.status == InvocationStatus.PLANNED, "Invocation must start planned")
            steps = await self.steps(invocation.run_id)
            require(
                any(
                    s.step_id == invocation.step_id
                    and s.task_id == invocation.task_id
                    and s.status == StepStatus.ACCEPTED
                    for s in steps
                ),
                "Invocation requires an accepted decision",
            )
            await self._db.execute(
                "INSERT INTO invocations VALUES (?,?,?,?,?,?,?,?)",
                (
                    invocation.invocation_id,
                    invocation.run_id,
                    invocation.task_id,
                    invocation.user_id,
                    invocation.step_id,
                    invocation.call_id,
                    invocation.status,
                    encode_invocation(invocation),
                ),
            )
        else:
            validate_transition(previous.status, invocation.status)
            require(
                replace(
                    previous,
                    status=invocation.status,
                    result=invocation.result,
                    error=invocation.error,
                )
                == invocation,
                "Invocation identity, arguments and approval binding are immutable",
            )
            await self._db.execute(
                "UPDATE invocations SET status=?,snapshot=? WHERE invocation_id=?",
                (invocation.status, encode_invocation(invocation), invocation.invocation_id),
            )
        await self._event(
            invocation.run_id,
            f"invocation.{invocation.status.value}",
            task_id=invocation.task_id,
            status=invocation.status,
        )

    async def get_approval(self, invocation_id: str) -> Approval | None:
        self.check_active()
        async with self._db.execute(
            "SELECT * FROM approvals WHERE invocation_id=? AND user_id=?",
            (invocation_id, self._user),
        ) as cursor:
            row = await cursor.fetchone()
        return (
            Approval(
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                bool(row[4]),
                datetime.fromisoformat(row[5]),
            )
            if row
            else None
        )

    @mutation
    async def save_approval(self, approval: Approval) -> None:
        self._scope(approval.user_id)
        invocation = await self.get_invocation(approval.invocation_id)
        require(invocation is not None, "Invocation unavailable")
        assert invocation is not None
        require(
            invocation.status == InvocationStatus.AWAITING_APPROVAL
            and invocation.argument_hash == approval.argument_hash
            and invocation.tool_version == approval.tool_version
            and invocation.approval_expires_at == approval.expires_at
            and approval.expires_at > self._clock.utc_now(),
            "Approval binding is invalid or expired",
        )
        await self._db.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?,?)",
            (
                approval.invocation_id,
                approval.user_id,
                approval.tool_version,
                approval.argument_hash,
                int(approval.approved),
                approval.expires_at.isoformat(),
            ),
        )
        await self.save_invocation(
            replace(
                invocation,
                status=InvocationStatus.READY if approval.approved else InvocationStatus.FAILED,
                error=None
                if approval.approved
                else JsonObject('{"code":"authorization","message":"Action denied"}'),
            ),
            InvocationStatus.AWAITING_APPROVAL,
        )

    async def artifacts(self, run_id: str) -> tuple[ArtifactRecord, ...]:
        self.check_active()
        async with self._db.execute(
            "SELECT snapshot FROM artifacts WHERE run_id=? AND user_id=? ORDER BY name,version",
            (run_id, self._user),
        ) as cursor:
            return tuple(decode_artifact(str(row[0])) for row in await cursor.fetchall())

    @mutation
    async def save_artifact(self, artifact: ArtifactRecord, expected: str | None) -> None:
        self._scope(artifact.user_id)
        previous = next(
            (
                a
                for a in await self.artifacts(artifact.run_id)
                if a.artifact_id == artifact.artifact_id
            ),
            None,
        )
        if (previous.status if previous else None) != expected:
            raise conflict()
        if previous is None:
            require(artifact.status == "staged", "New manifest must be staged")
            await self._db.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)",
                (
                    artifact.artifact_id,
                    artifact.run_id,
                    artifact.task_id,
                    artifact.user_id,
                    artifact.name,
                    artifact.version,
                    artifact.status,
                    encode_artifact(artifact),
                ),
            )
        else:
            require(
                previous.status == "staged"
                and artifact.status == "committed"
                and replace(previous, status="committed") == artifact,
                "Invalid manifest transition",
            )
            await self._db.execute(
                "UPDATE artifacts SET status=?,snapshot=? WHERE artifact_id=?",
                (artifact.status, encode_artifact(artifact), artifact.artifact_id),
            )
        await self._event(artifact.run_id, f"artifact.{artifact.status}", task_id=artifact.task_id)

    @mutation
    async def create_step(self, step: Step) -> None:
        self._scope(step.user_id)
        require(step.status == StepStatus.REQUESTED, "New step must be requested")
        task = await self.get_task(step.task_id)
        require(
            task is not None and task.run_id == step.run_id and task.status == TaskStatus.RUNNING,
            "Step requires a running scoped task",
        )
        previous = await self.steps(step.run_id, step.task_id)
        require(step.ordinal == len(previous) + 1, "Step ordinals must be contiguous")
        require(all(s.status == StepStatus.APPLIED for s in previous), "Prior step is unresolved")
        await self._db.execute(
            "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                step.step_id,
                step.task_id,
                step.run_id,
                step.user_id,
                step.ordinal,
                step.status,
                encode_step(step),
            ),
        )
        await self._event(
            step.run_id,
            "step.requested",
            task_id=step.task_id,
            version=step.ordinal,
            status=step.status,
        )

    async def steps(self, run_id: str, task_id: str | None = None) -> tuple[Step, ...]:
        self.check_active()
        async with self._db.execute(
            "SELECT snapshot FROM steps WHERE run_id=? AND user_id=? "
            "AND (? IS NULL OR task_id=?) ORDER BY ordinal",
            (run_id, self._user, task_id, task_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(decode_step(str(row[0])) for row in rows)

    @mutation
    async def update_step(
        self,
        step_id: str,
        expected: StepStatus,
        status: StepStatus,
        *,
        response: ModelResponse | None = None,
        error: JsonObject | None = None,
    ) -> Step:
        self.check_active()
        async with self._db.execute(
            "SELECT snapshot FROM steps WHERE step_id=? AND user_id=?", (step_id, self._user)
        ) as cursor:
            row = await cursor.fetchone()
        require(row is not None, "Step is unavailable")
        assert row is not None
        step = decode_step(str(row[0]))
        if step.status != expected:
            raise conflict()
        allowed = {
            StepStatus.REQUESTED: {StepStatus.ACCEPTED, StepStatus.FAILED},
            StepStatus.ACCEPTED: {StepStatus.APPLIED},
            StepStatus.APPLIED: set(),
            StepStatus.FAILED: set(),
        }
        require(status in allowed[expected], "Invalid step transition")
        require(
            response is None or expected == StepStatus.REQUESTED, "Accepted decisions are immutable"
        )
        updated = replace(step, status=status, response=response or step.response, error=error)
        await self._db.execute(
            "UPDATE steps SET status=?,snapshot=? WHERE step_id=? AND user_id=?",
            (status, encode_step(updated), step_id, self._user),
        )
        await self._event(
            step.run_id,
            f"step.{status.value}",
            task_id=step.task_id,
            version=step.ordinal,
            status=status,
        )
        return updated

    async def _event(
        self,
        run_id: str,
        event_type: str,
        *,
        task_id: str | None = None,
        version: int = 0,
        status: str | None = None,
        intent: ControlIntent | None = None,
    ) -> None:
        self.check_active()
        # Only harness-authored status metadata is persisted; inputs/config/results are excluded.
        async with self._db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            sequence = int(row[0])
        event = Event(
            self._ids.new_id(),
            run_id,
            self._user,
            sequence,
            event_type,
            self._clock.utc_now(),
            JsonObject(json.dumps({"version": version, "status": status, "intent": intent})),
            task_id,
        )
        await self._db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                run_id,
                self._user,
                sequence,
                task_id,
                event.schema_version,
                event_type,
                event.timestamp.isoformat(),
                event.payload.text,
            ),
        )

    @mutation
    async def create_run(self, run: Run) -> None:
        self._scope(run.user_id)
        require(
            run.version == 0
            and run.status == RunStatus.CREATED
            and run.intent == ControlIntent.NONE
            and run.result is None
            and run.error is None,
            "New runs must be pristine",
        )
        await self._db.execute(
            "INSERT INTO runs(run_id,user_id,status,version,snapshot,control_intent) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run.run_id, run.user_id, run.status, run.version, encode(run), run.intent),
        )
        await self._event(run.run_id, "run.created", status=run.status)

    async def get_run(self, run_id: str) -> Run | None:
        self.check_active()
        async with self._db.execute(
            "SELECT snapshot FROM runs WHERE run_id = ? AND user_id = ?", (run_id, self._user)
        ) as cursor:
            row = await cursor.fetchone()
        return decode_run(str(row[0])) if row else None

    async def _run(self, run_id: str, version: int) -> Run:
        run = await self.get_run(run_id)
        if run is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Run is unavailable to this principal")
        if run.version != version:
            raise conflict()
        return run

    async def _save_run(self, run: Run, previous: int, event_type: str) -> Run:
        async with self._db.execute(
            "UPDATE runs SET status=?,version=?,snapshot=?,control_intent=? "
            "WHERE run_id=? AND user_id=? AND version=?",
            (run.status, run.version, encode(run), run.intent, run.run_id, self._user, previous),
        ) as cursor:
            if cursor.rowcount != 1:
                raise conflict()
        await self._event(
            run.run_id, event_type, version=run.version, status=run.status, intent=run.intent
        )
        return run

    @mutation
    async def transition_run(
        self,
        run_id: str,
        expected_version: int,
        status: RunStatus,
        *,
        result: JsonObject | None = None,
        error: JsonObject | None = None,
    ) -> Run:
        run = await self._run(run_id, expected_version)
        validate_transition(run.status, status)
        updated = replace(
            run,
            status=status,
            result=result,
            error=error,
            version=run.version + 1,
            updated_at=self._clock.utc_now(),
        )
        return await self._save_run(updated, expected_version, "run.transitioned")

    @mutation
    async def set_intent(self, run_id: str, expected_version: int, intent: ControlIntent) -> Run:
        run = await self._run(run_id, expected_version)
        require(bool(RUN_TRANSITIONS[run.status]), "Terminal runs are immutable")
        require(run.intent != ControlIntent.CANCEL, "Cancellation intent cannot be revoked")
        require(run.intent != intent, "Intent is already recorded")
        updated = replace(
            run, intent=intent, version=run.version + 1, updated_at=self._clock.utc_now()
        )
        return await self._save_run(updated, expected_version, "run.intent_changed")

    @mutation
    async def create_task(self, task: Task) -> None:
        self._scope(task.user_id)
        run = await self.get_run(task.run_id)
        require(
            run is not None and bool(RUN_TRANSITIONS[run.status]), "Run is unavailable or terminal"
        )
        require(
            task.version == 0 and task.status == TaskStatus.PENDING and task.result is None,
            "New tasks must be pristine",
        )
        require(task.error is None, "New tasks cannot have an error")
        # Phase 1 only references already-created tasks. This prevents cycles by construction.
        for related in (*task.dependencies, task.parent_task_id):
            if related is not None:
                existing = await self.get_task(related)
                require(
                    existing is not None and existing.run_id == task.run_id,
                    "Related task must exist in the same run",
                )
        await self._db.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task.task_id,
                task.run_id,
                task.user_id,
                task.parent_task_id,
                task.status,
                task.version,
                encode(task),
            ),
        )
        for dependency in task.dependencies:
            await self._db.execute(
                "INSERT INTO task_dependencies VALUES (?, ?, ?, ?)",
                (task.task_id, dependency, task.run_id, task.user_id),
            )
        await self._event(task.run_id, "task.created", task_id=task.task_id, status=task.status)

    async def get_task(self, task_id: str) -> Task | None:
        self.check_active()
        async with self._db.execute(
            "SELECT snapshot FROM tasks WHERE task_id=? AND user_id=?", (task_id, self._user)
        ) as cursor:
            row = await cursor.fetchone()
        return decode_task(str(row[0])) if row else None

    async def tasks(self, run_id: str) -> tuple[Task, ...]:
        self.check_active()
        async with self._db.execute(
            "SELECT snapshot FROM tasks WHERE run_id=? AND user_id=? "
            "ORDER BY rowid",
            (run_id, self._user),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(decode_task(str(row[0])) for row in rows)

    async def delegated_task(self, parent_task_id: str, idempotency_key: str) -> Task | None:
        self.check_active()
        async with self._db.execute(
            "SELECT t.snapshot FROM delegations d JOIN tasks t ON t.task_id=d.child_task_id "
            "WHERE d.parent_task_id=? AND d.idempotency_key=? AND d.user_id=?",
            (parent_task_id, idempotency_key, self._user),
        ) as cursor:
            row = await cursor.fetchone()
        return decode_task(str(row[0])) if row else None

    @mutation
    async def save_delegation(
        self, parent_task_id: str, idempotency_key: str, task: Task
    ) -> None:
        self._scope(task.user_id)
        parent = await self.get_task(parent_task_id)
        require(
            parent is not None
            and task.parent_task_id == parent_task_id
            and task.run_id == parent.run_id,
            "Delegation parent and child must belong to the same run",
        )
        require(bool(idempotency_key.strip()), "Delegation idempotency key is required")
        await self._db.execute(
            "INSERT INTO delegations VALUES (?, ?, ?, ?, ?)",
            (parent_task_id, idempotency_key, task.task_id, task.run_id, self._user),
        )

    @mutation
    async def transition_task(
        self,
        task_id: str,
        expected_version: int,
        status: TaskStatus,
        *,
        result: JsonObject | None = None,
        error: JsonObject | None = None,
    ) -> Task:
        task = await self.get_task(task_id)
        if task is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Task is unavailable to this principal")
        if task.version != expected_version:
            raise conflict()
        validate_transition(task.status, status)
        updated = replace(
            task,
            status=status,
            result=result,
            error=error,
            version=task.version + 1,
            updated_at=self._clock.utc_now(),
        )
        async with self._db.execute(
            "UPDATE tasks SET status=?,version=?,snapshot=? "
            "WHERE task_id=? AND user_id=? AND version=?",
            (status, updated.version, encode(updated), task_id, self._user, expected_version),
        ) as cursor:
            if cursor.rowcount != 1:
                raise conflict()
        await self._event(
            task.run_id,
            "task.transitioned",
            task_id=task_id,
            version=updated.version,
            status=status,
        )
        return updated

    @mutation
    async def set_task_handoff(
        self, task_id: str, expected_version: int, handoff: JsonObject
    ) -> Task:
        task = await self.get_task(task_id)
        if task is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Task is unavailable to this principal")
        if task.version != expected_version:
            raise conflict()
        require(task.status == TaskStatus.READY, "Handoff can only be finalized before execution")
        updated = replace(
            task,
            handoff=handoff,
            version=task.version + 1,
            updated_at=self._clock.utc_now(),
        )
        async with self._db.execute(
            "UPDATE tasks SET version=?,snapshot=? WHERE task_id=? AND user_id=? AND version=?",
            (updated.version, encode(updated), task_id, self._user, expected_version),
        ) as cursor:
            if cursor.rowcount != 1:
                raise conflict()
        await self._event(
            task.run_id,
            "task.handoff_finalized",
            task_id=task_id,
            version=updated.version,
        )
        return updated

    async def events(self, run_id: str, *, after: int = 0, limit: int = 100) -> tuple[Event, ...]:
        self.check_active()
        require(after >= 0 and 1 <= limit <= 1000, "Invalid event pagination")
        async with self._db.execute(
            "SELECT event_id,run_id,user_id,sequence,event_type,timestamp,payload,task_id,"
            "schema_version FROM events WHERE run_id=? AND user_id=? AND sequence>? "
            "ORDER BY sequence LIMIT ?",
            (run_id, self._user, after, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(
            Event(
                str(r[0]),
                str(r[1]),
                str(r[2]),
                int(r[3]),
                str(r[4]),
                datetime.fromisoformat(r[5]),
                JsonObject(r[6]),
                r[7],
                int(r[8]),
            )
            for r in rows
        )

    async def load_checkpoint(self, task_id: str) -> Checkpoint | None:
        self.check_active()
        async with self._db.execute(
            "SELECT task_id,run_id,user_id,state_version,last_event_sequence,continuation,"
            "version,schema_version FROM checkpoints WHERE task_id=? AND user_id=?",
            (task_id, self._user),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return Checkpoint(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            int(row[3]),
            int(row[4]),
            JsonObject(row[5]),
            int(row[6]),
            int(row[7]),
        )

    @mutation
    async def save_checkpoint(self, checkpoint: Checkpoint, expected_version: int | None) -> None:
        self._scope(checkpoint.user_id)
        task = await self.get_task(checkpoint.task_id)
        require(
            task is not None and task.run_id == checkpoint.run_id, "Checkpoint task is unavailable"
        )
        assert task is not None
        if task.version != checkpoint.state_version:
            raise conflict()
        previous = await self.load_checkpoint(checkpoint.task_id)
        if (previous.version if previous else None) != expected_version:
            raise conflict()
        require(
            checkpoint.version == (0 if previous is None else previous.version + 1),
            "Checkpoint version must advance exactly once",
        )
        if previous is not None:
            require(
                checkpoint.last_event_sequence >= previous.last_event_sequence,
                "Checkpoint event cursor cannot move backwards",
            )
        async with self._db.execute(
            "SELECT MAX(sequence) FROM events WHERE run_id=? AND task_id=?",
            (task.run_id, task.task_id),
        ) as cursor:
            row = await cursor.fetchone()
        require(
            row is not None
            and row[0] is not None
            and checkpoint.last_event_sequence >= int(row[0]),
            "Checkpoint misses task events",
        )
        await self._db.execute(
            "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET schema_version=excluded.schema_version,"
            "version=excluded.version,state_version=excluded.state_version,"
            "last_event_sequence=excluded.last_event_sequence,continuation=excluded.continuation",
            (
                checkpoint.task_id,
                checkpoint.run_id,
                checkpoint.user_id,
                checkpoint.schema_version,
                checkpoint.version,
                checkpoint.state_version,
                checkpoint.last_event_sequence,
                checkpoint.continuation.text,
            ),
        )
