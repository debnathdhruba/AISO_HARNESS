"""Submission and inspection; no transport or provider dependencies."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from agent_harness.application.routing import select_mode
from agent_harness.domain.entities import JsonObject, Run, RunConfig, Task
from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import RunOptions
from agent_harness.domain.states import ControlIntent, RunStatus, TaskStatus
from agent_harness.ports.state import Clock, IdGenerator, StateStore


class RunService:
    def __init__(self, store: StateStore, clock: Clock, ids: IdGenerator) -> None:
        self._store, self._clock, self._ids = store, clock, ids

    async def submit(self, user_id: str, task_input: str, options: RunOptions) -> Run:
        options = replace(
            options,
            workspace=str(Path(options.workspace).absolute()),
            artifact_root=str(Path(options.artifact_root).absolute()),
            routing_mode=select_mode(task_input, options),
        )
        now = self._clock.utc_now()
        task_id = self._ids.new_id()
        config = RunConfig(
            "coordinator",
            "1",
            model=options.snapshot(),
            policy=JsonObject(json.dumps({"root_task_id": task_id})),
            budgets=JsonObject(
                json.dumps(
                    {
                        "max_steps": options.max_steps,
                        "max_total_steps": options.max_total_steps,
                        "max_total_tokens": options.max_total_tokens,
                        "max_total_tool_calls": options.max_total_tool_calls,
                        "retry_strategy": options.retry_strategy,
                        "max_retries": options.max_retries,
                        "max_corrections": options.max_corrections,
                        "partial_result_policy": options.partial_result_policy,
                    }
                )
            ),
            memory=JsonObject(
                json.dumps(
                    {
                        "retrieval_enabled": options.memory_retrieval_enabled,
                        "extraction_enabled": options.memory_extraction_enabled,
                        "retrieval_limit": options.memory_retrieval_limit,
                        "context_bytes": options.memory_context_bytes,
                    }
                )
            ),
        )
        run = Run(
            self._ids.new_id(),
            user_id,
            task_input,
            config,
            now,
            now,
            deadline=now + timedelta(seconds=options.timeout_seconds),
        )
        capabilities = set(options.tool_allowlist)
        if options.memory_extraction_enabled:
            capabilities.add("memory_save")
        else:
            capabilities.discard("memory_save")
        task = Task(
            task_id,
            run.run_id,
            user_id,
            task_input,
            now,
            now,
            acceptance_criteria=JsonObject(
                json.dumps(
                    {
                        "required_artifacts": options.required_artifacts,
                        "result_schema": json.loads(options.result_schema.text),
                    }
                )
            ),
            deadline=run.deadline,
            agent_instance_id=self._ids.new_id(),
            agent_definition_id="coordinator",
            agent_definition_version="1",
            capabilities=tuple(sorted(capabilities)),
        )
        async with self._store.transaction(user_id) as tx:
            await tx.create_run(run)
            await tx.create_task(task)
            await tx.transition_task(task_id, 0, TaskStatus.READY)
            return await tx.transition_run(run.run_id, 0, RunStatus.QUEUED)

    async def inspect(self, user_id: str, run_id: str) -> Run:
        async with self._store.transaction(user_id) as tx:
            run = await tx.get_run(run_id)
        if run is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Run is unavailable to this principal")
        return run

    async def request_control(self, user_id: str, run_id: str, intent: ControlIntent) -> Run:
        if intent == ControlIntent.NONE:
            raise HarnessError(ErrorCode.VALIDATION, "Control intent cannot be empty")
        async with self._store.transaction(user_id) as tx:
            run = await tx.get_run(run_id)
            if run is None:
                raise HarnessError(ErrorCode.AUTHORIZATION, "Run is unavailable to this principal")
            return await tx.set_intent(run_id, run.version, intent)

    async def resume(self, user_id: str, run_id: str) -> Run:
        async with self._store.transaction(user_id) as tx:
            run = await tx.get_run(run_id)
            if run is None:
                raise HarnessError(ErrorCode.AUTHORIZATION, "Run is unavailable to this principal")
            if run.status != RunStatus.PAUSED or run.intent != ControlIntent.PAUSE:
                raise HarnessError(
                    ErrorCode.VALIDATION,
                    "Only an explicitly paused run can be resumed",
                )
            if run.deadline is not None and run.deadline <= self._clock.utc_now():
                raise HarnessError(
                    ErrorCode.DEADLINE_EXCEEDED, "Run deadline expired before resume"
                )
            for task in await tx.tasks(run_id):
                if task.status == TaskStatus.PAUSED:
                    await tx.transition_task(task.task_id, task.version, TaskStatus.READY)
            run = await tx.set_intent(run_id, run.version, ControlIntent.NONE)
            return await tx.transition_run(run_id, run.version, RunStatus.QUEUED)
