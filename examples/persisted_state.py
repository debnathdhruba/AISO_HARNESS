"""Phase 1 state demonstration, not an agent execution loop."""

import argparse
import asyncio
from pathlib import Path

from agent_harness.adapters.system import SystemClock, UuidGenerator
from agent_harness.domain.entities import Checkpoint, JsonObject, Run, RunConfig, Task
from agent_harness.domain.states import RunStatus, TaskStatus
from agent_harness.persistence.sqlite import SQLiteStore


async def demonstrate(path: Path) -> None:
    clock, ids = SystemClock(), UuidGenerator()
    now = clock.utc_now()
    run = Run(ids.new_id(), "local-user", "Inspect durable state", RunConfig("demo", "1"), now, now)
    task = Task(ids.new_id(), run.run_id, run.user_id, run.task_input, now, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with SQLiteStore(path, clock, ids) as store:
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
    async with SQLiteStore(path, clock, ids) as reopened:
        async with reopened.transaction(run.user_id) as tx:
            restored = await tx.get_run(run.run_id)
            checkpoint = await tx.load_checkpoint(task.task_id)
            assert restored is not None and checkpoint is not None
            print(f"run_id={restored.run_id} status={restored.status} version={restored.version}")
            print(f"checkpoint: state_version={checkpoint.state_version}")
            for event in await tx.events(run.run_id):
                print(f"event {event.sequence}: {event.event_type}")
            print(f"Reopened persisted state: {path.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(".local/harness.sqlite"))
    args = parser.parse_args()
    asyncio.run(demonstrate(args.database))
