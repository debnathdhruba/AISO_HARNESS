"""Ordered transactional migrations; version 2 adds durable control intent."""

import aiosqlite

from agent_harness.domain.errors import ErrorCode, HarnessError

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version >= 0), snapshot TEXT NOT NULL,
            UNIQUE(run_id, user_id))""",
        """CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, user_id TEXT NOT NULL,
            parent_task_id TEXT, status TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version >= 0), snapshot TEXT NOT NULL,
            UNIQUE(task_id, run_id, user_id),
            FOREIGN KEY(run_id, user_id) REFERENCES runs(run_id, user_id),
            FOREIGN KEY(parent_task_id, run_id, user_id)
                REFERENCES tasks(task_id, run_id, user_id))""",
        """CREATE TABLE task_dependencies (
            task_id TEXT NOT NULL, dependency_id TEXT NOT NULL, run_id TEXT NOT NULL,
            user_id TEXT NOT NULL, PRIMARY KEY(task_id, dependency_id),
            CHECK(task_id != dependency_id),
            FOREIGN KEY(task_id, run_id, user_id) REFERENCES tasks(task_id, run_id, user_id),
            FOREIGN KEY(dependency_id, run_id, user_id)
                REFERENCES tasks(task_id, run_id, user_id))""",
        """CREATE TABLE events (
            event_id TEXT UNIQUE NOT NULL, run_id TEXT NOT NULL, user_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence > 0), task_id TEXT,
            schema_version INTEGER NOT NULL, event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(run_id, sequence),
            FOREIGN KEY(run_id, user_id) REFERENCES runs(run_id, user_id),
            FOREIGN KEY(task_id, run_id, user_id) REFERENCES tasks(task_id, run_id, user_id))""",
        """CREATE TABLE checkpoints (
            task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, user_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL, version INTEGER NOT NULL CHECK(version >= 0),
            state_version INTEGER NOT NULL CHECK(state_version >= 0),
            last_event_sequence INTEGER NOT NULL, continuation TEXT NOT NULL,
            FOREIGN KEY(task_id, run_id, user_id) REFERENCES tasks(task_id, run_id, user_id),
            FOREIGN KEY(run_id, last_event_sequence) REFERENCES events(run_id, sequence))""",
        "CREATE INDEX runs_user_status ON runs(user_id, status)",
        "CREATE INDEX tasks_run_status ON tasks(run_id, status)",
        "CREATE INDEX events_user_run_sequence ON events(user_id, run_id, sequence)",
        """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
            BEGIN SELECT RAISE(ABORT, 'Events are append only'); END""",
        """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
            BEGIN SELECT RAISE(ABORT, 'Events are append only'); END""",
    ),
    ("ALTER TABLE runs ADD COLUMN control_intent TEXT NOT NULL DEFAULT 'none'",),
    (
        """CREATE TABLE steps (
            step_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
            user_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal > 0),
            status TEXT NOT NULL, snapshot TEXT NOT NULL,
            UNIQUE(task_id, ordinal),
            FOREIGN KEY(task_id, run_id, user_id) REFERENCES tasks(task_id, run_id, user_id))""",
        "CREATE INDEX steps_run_ordinal ON steps(run_id, ordinal)",
    ),
    (
        """CREATE TABLE invocations (
            invocation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
            user_id TEXT NOT NULL, step_id TEXT NOT NULL REFERENCES steps(step_id),
            call_id TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT NOT NULL,
            UNIQUE(step_id, call_id),
            FOREIGN KEY(task_id,run_id,user_id) REFERENCES tasks(task_id,run_id,user_id))""",
        """CREATE TABLE approvals (
            invocation_id TEXT PRIMARY KEY REFERENCES invocations(invocation_id),
            user_id TEXT NOT NULL, tool_version TEXT NOT NULL, argument_hash TEXT NOT NULL,
            approved INTEGER NOT NULL, expires_at TEXT NOT NULL)""",
        "CREATE INDEX invocations_run ON invocations(user_id,run_id)",
    ),
    (
        """CREATE TABLE delegations (
            parent_task_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
            child_task_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, user_id TEXT NOT NULL,
            PRIMARY KEY(parent_task_id, idempotency_key),
            FOREIGN KEY(parent_task_id, run_id, user_id)
                REFERENCES tasks(task_id, run_id, user_id),
            FOREIGN KEY(child_task_id, run_id, user_id)
                REFERENCES tasks(task_id, run_id, user_id))""",
        "CREATE INDEX delegations_run ON delegations(user_id, run_id, parent_task_id)",
    ),
)


async def migrate(connection: aiosqlite.Connection) -> None:
    await connection.execute("BEGIN IMMEDIATE")
    try:
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
        )
        async with connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ) as c:
            versions = [int(row[0]) for row in await c.fetchall()]
        if versions != list(range(1, len(versions) + 1)) or len(versions) > len(MIGRATIONS):
            raise HarnessError(ErrorCode.STORAGE, "Unsupported database schema history")
        for index in range(len(versions), len(MIGRATIONS)):
            for statement in MIGRATIONS[index]:
                await connection.execute(statement)
            await connection.execute("INSERT INTO schema_migrations VALUES (?)", (index + 1,))
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise
