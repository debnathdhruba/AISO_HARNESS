"""Phase 1 state demonstration, not an agent execution loop."""

import argparse
import asyncio

from agent_harness.adapters.system import SystemClock, UuidGenerator
from agent_harness.config import load_settings
from agent_harness.domain.entities import Checkpoint, JsonObject, Run, RunConfig, Task
from agent_harness.domain.states import RunStatus, TaskStatus
from agent_harness.persistence.mongo import MongoStore


async def demonstrate(database_name: str) -> None:
    clock, ids = SystemClock(), UuidGenerator()
    settings = load_settings(database_name=database_name)
    now = clock.utc_now()
    run = Run(ids.new_id(), "local-user", "Inspect durable state", RunConfig("demo", "1"), now, now)
    task = Task(ids.new_id(), run.run_id, run.user_id, run.task_input, now, now)
    async with MongoStore(
        settings.mongodb_uri.get_secret_value(), database_name, clock, ids
    ) as store:
        async with store.transaction(run.user_id) as tx:
            await tx.create_run(run)
            await tx.create_task(task)
            await tx.transition_run(run.run_id, 0, RunStatus.QUEUED)
            task = await tx.transition_task(task.task_id, 0, TaskStatus.READY)
            events = await tx.events(run.run_id)
            await tx.save_checkpoint(
                Checkpoint(
                    task.task_id,
                    run.run_id,
                    run.user_id,
                    task.version,
                    events[-1].sequence,
                    JsonObject('{"next":"await_phase_2"}'),
                ),
                None,
            )
    async with MongoStore(
        settings.mongodb_uri.get_secret_value(), database_name, clock, ids
    ) as reopened:
        async with reopened.transaction(run.user_id) as tx:
            restored = await tx.get_run(run.run_id)
            checkpoint = await tx.load_checkpoint(task.task_id)
            assert restored is not None and checkpoint is not None
            print(f"run_id={restored.run_id} status={restored.status} version={restored.version}")
            print(f"checkpoint: state_version={checkpoint.state_version}")
            for event in await tx.events(run.run_id):
                print(f"event {event.sequence}: {event.event_type}")
            print(f"Reopened MongoDB persisted state: {database_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-name", default="test")
    args = parser.parse_args()
    asyncio.run(demonstrate(args.database_name))
