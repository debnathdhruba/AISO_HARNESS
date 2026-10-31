import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from conftest import FixedClock, SequenceIds

from agent_harness.domain.entities import Checkpoint, JsonObject, Run, Task
from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.states import ControlIntent, RunStatus, TaskStatus
from agent_harness.persistence.codec import encode
from agent_harness.persistence.migrations import MIGRATIONS
from agent_harness.persistence.sqlite import SQLiteStore

pytestmark = pytest.mark.asyncio


async def seed(store: SQLiteStore, run: Run, task: Task) -> None:
    async with store.transaction(run.user_id) as tx:
        await tx.create_run(run)
        await tx.create_task(task)


async def test_atomic_state_events_checkpoint_reopen(tmp_path: Path, run: Run, task: Task) -> None:
    path = tmp_path / "reopen.sqlite"
    async with SQLiteStore(path, FixedClock(), SequenceIds()) as store:
        async with store.transaction(run.user_id) as tx:
            await tx.create_run(run)
            await tx.create_task(task)
            updated = await tx.transition_task(task.task_id, 0, TaskStatus.READY)
            events = await tx.events(run.run_id)
            checkpoint = Checkpoint(
                task.task_id,
                run.run_id,
                run.user_id,
                updated.version,
                events[-1].sequence,
                JsonObject('{"next":"model"}'),
            )
            await tx.save_checkpoint(checkpoint, None)
    async with SQLiteStore(path, FixedClock(), SequenceIds()) as store:
        async with store.transaction(run.user_id) as tx:
            assert await tx.get_run(run.run_id) == run
            assert await tx.get_task(task.task_id) == updated
            assert await tx.load_checkpoint(task.task_id) == checkpoint
            assert [e.sequence for e in await tx.events(run.run_id)] == [1, 2, 3]
            assert [e.event_type for e in await tx.events(run.run_id, after=1, limit=1)] == [
                "task.created"
            ]
            assert all(run.task_input not in e.payload.text for e in events)


async def test_exception_rolls_back_entire_unit(store: SQLiteStore, run: Run, task: Task) -> None:
    with pytest.raises(RuntimeError):
        async with store.transaction(run.user_id) as tx:
            await tx.create_run(run)
            await tx.create_task(task)
            raise RuntimeError("injected crash before commit")
    async with store.transaction(run.user_id) as tx:
        assert await tx.get_run(run.run_id) is None
        assert await tx.get_task(task.task_id) is None
        assert await tx.events(run.run_id) == ()


async def test_event_write_failure_cannot_commit_state(tmp_path: Path, run: Run) -> None:
    class BrokenIds:
        def new_id(self) -> str:
            raise RuntimeError("injected event failure")

    async with SQLiteStore(tmp_path / "fault.sqlite", FixedClock(), BrokenIds()) as store:
        with pytest.raises(HarnessError, match="roll back"):
            async with store.transaction(run.user_id) as tx:
                # Even a caller swallowing the event error cannot commit the earlier INSERT.
                with pytest.raises(RuntimeError):
                    await tx.create_run(run)
        async with store.transaction(run.user_id) as tx:
            assert await tx.get_run(run.run_id) is None
            assert await tx.events(run.run_id) == ()


async def test_optimistic_conflict_preserves_events(
    store: SQLiteStore, run: Run, task: Task
) -> None:
    await seed(store, run, task)
    async with store.transaction(run.user_id) as tx:
        await tx.transition_run(run.run_id, 0, RunStatus.QUEUED)
    with pytest.raises(HarnessError) as failure:
        async with store.transaction(run.user_id) as tx:
            await tx.transition_run(run.run_id, 0, RunStatus.RUNNING)
    assert failure.value.code == ErrorCode.CONCURRENCY_CONFLICT
    async with store.transaction(run.user_id) as tx:
        current = await tx.get_run(run.run_id)
        assert current is not None and current.status == RunStatus.QUEUED and current.version == 1
        assert len(await tx.events(run.run_id)) == 3


async def test_user_scope_including_direct_ids(store: SQLiteStore, run: Run, task: Task) -> None:
    await seed(store, run, task)
    async with store.transaction("bob") as tx:
        assert await tx.get_run(run.run_id) is None
        assert await tx.get_task(task.task_id) is None
        assert await tx.events(run.run_id) == ()
        assert await tx.load_checkpoint(task.task_id) is None
    with pytest.raises(HarnessError) as failure:
        async with store.transaction("bob") as tx:
            await tx.transition_run(run.run_id, 0, RunStatus.QUEUED)
    assert failure.value.code == ErrorCode.AUTHORIZATION
    with pytest.raises(HarnessError):
        async with store.transaction("bob") as tx:
            await tx.create_run(run)


async def test_intent_is_separate_from_status(store: SQLiteStore, run: Run, task: Task) -> None:
    await seed(store, run, task)
    async with store.transaction(run.user_id) as tx:
        changed = await tx.set_intent(run.run_id, 0, ControlIntent.CANCEL)
        assert changed.status == RunStatus.CREATED
        assert changed.intent == ControlIntent.CANCEL
        assert changed.version == 1
        assert '"intent":"cancel"' in (await tx.events(run.run_id))[-1].payload.text
    with pytest.raises(HarnessError):
        async with store.transaction(run.user_id) as tx:
            await tx.set_intent(run.run_id, 1, ControlIntent.NONE)
    async with store.transaction(run.user_id) as tx:
        await tx.transition_run(run.run_id, 1, RunStatus.CANCELLED)
    with pytest.raises(HarnessError):
        async with store.transaction(run.user_id) as tx:
            await tx.transition_run(run.run_id, 2, RunStatus.QUEUED)


async def test_checkpoint_versions_and_atomic_rollback(
    store: SQLiteStore, run: Run, task: Task
) -> None:
    await seed(store, run, task)
    checkpoint = Checkpoint(task.task_id, run.run_id, run.user_id, 0, 2, JsonObject())
    async with store.transaction(run.user_id) as tx:
        await tx.save_checkpoint(checkpoint, None)
    with pytest.raises(HarnessError) as failure:
        async with store.transaction(run.user_id) as tx:
            await tx.transition_task(task.task_id, 0, TaskStatus.READY)
            await tx.save_checkpoint(checkpoint, 0)
    assert failure.value.code == ErrorCode.CONCURRENCY_CONFLICT
    async with store.transaction(run.user_id) as tx:
        assert await tx.get_task(task.task_id) == task
        assert len(await tx.events(run.run_id)) == 2
        await tx.save_checkpoint(replace(checkpoint, version=1), 0)
    with pytest.raises(HarnessError) as failure:
        async with store.transaction(run.user_id) as tx:
            await tx.save_checkpoint(replace(checkpoint, version=1), 0)
    assert failure.value.code == ErrorCode.CONCURRENCY_CONFLICT


async def test_coroutines_do_not_share_transactions(
    store: SQLiteStore, run: Run, task: Task
) -> None:
    first_inside, second_attempted, release_first = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    order: list[str] = []

    async def first() -> None:
        with pytest.raises(RuntimeError):
            async with store.transaction(run.user_id) as tx:
                await tx.create_run(run)
                first_inside.set()
                await release_first.wait()  # Deliberately hold a transaction for isolation testing.
                order.append("first_rollback")
                raise RuntimeError("rollback")

    async def second() -> None:
        await first_inside.wait()
        second_attempted.set()
        async with store.transaction(run.user_id) as tx:
            order.append("second_entered")
            assert await tx.get_run(run.run_id) is None
            await tx.create_run(run)
            await tx.create_task(task)

    async with asyncio.timeout(5):
        async with asyncio.TaskGroup() as group:
            group.create_task(first())
            group.create_task(second())
            await second_attempted.wait()
            assert order == []
            release_first.set()
    assert order == ["first_rollback", "second_entered"]
    async with store.transaction(run.user_id) as tx:
        assert [event.sequence for event in await tx.events(run.run_id)] == [1, 2]


async def test_cancelled_transaction_rolls_back_and_releases_lock(
    store: SQLiteStore, run: Run
) -> None:
    entered = asyncio.Event()
    never = asyncio.Event()

    async def writer() -> None:
        async with store.transaction(run.user_id) as tx:
            await tx.create_run(run)
            entered.set()
            await never.wait()

    async with asyncio.timeout(5):
        worker = asyncio.create_task(writer())
        await entered.wait()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        async with store.transaction(run.user_id) as tx:
            assert await tx.get_run(run.run_id) is None
            await tx.create_run(run)


async def test_transaction_lifecycle_and_coroutine_ownership(store: SQLiteStore, run: Run) -> None:
    async with store.transaction(run.user_id) as tx:
        with pytest.raises(HarnessError, match="Nested"):
            async with store.transaction(run.user_id):
                pass
        with pytest.raises(HarnessError, match="another coroutine"):
            await asyncio.create_task(tx.get_run(run.run_id))
    with pytest.raises(HarnessError, match="closed"):
        await tx.get_run(run.run_id)


async def test_migrate_existing_version_one_database(tmp_path: Path, run: Run) -> None:
    path = tmp_path / "old.sqlite"
    # A real previous-schema database with a stored snapshot, not just an empty version marker.
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
        for statement in MIGRATIONS[0]:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_migrations VALUES (1)")
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
            (run.run_id, run.user_id, run.status, run.version, encode(run)),
        )
    async with SQLiteStore(path, FixedClock(), SequenceIds()) as store:
        async with store.transaction(run.user_id) as tx:
            assert await tx.get_run(run.run_id) == run
            await tx.set_intent(run.run_id, 0, ControlIntent.PAUSE)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,)]
        assert connection.execute("SELECT control_intent FROM runs").fetchone() == ("pause",)
    # Reopen must not apply migration 2 twice.
    async with SQLiteStore(path, FixedClock(), SequenceIds()):
        pass


async def test_future_schema_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
        connection.executemany("INSERT INTO schema_migrations VALUES (?)", [(1,), (2,), (3,), (4,)])
    with pytest.raises(HarnessError, match="Unsupported database schema"):
        async with SQLiteStore(path, FixedClock(), SequenceIds()):
            pass


async def test_relationship_scope_and_sqlite_invariants(
    tmp_path: Path, run: Run, task: Task
) -> None:
    path = tmp_path / "relations.sqlite"
    async with SQLiteStore(path, FixedClock(), SequenceIds()) as store:
        await seed(store, run, task)
        other_run = replace(run, run_id=str(UUID(int=3)))
        async with store.transaction(run.user_id) as tx:
            await tx.create_run(other_run)
        with pytest.raises(HarnessError, match="same run"):
            async with store.transaction(run.user_id) as tx:
                await tx.create_task(
                    replace(
                        task,
                        task_id=str(UUID(int=4)),
                        run_id=other_run.run_id,
                        parent_task_id=task.task_id,
                    )
                )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE events SET payload='{}'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO task_dependencies VALUES (?, ?, ?, ?)",
                (task.task_id, str(UUID(int=99)), run.run_id, run.user_id),
            )
