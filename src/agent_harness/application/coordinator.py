"""Durable, bounded child-task creation and dependency-aware execution."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from agent_harness.agents.factory import AgentFactory
from agent_harness.agents.scheduler import TaskScheduler
from agent_harness.domain.entities import JsonObject, Task
from agent_harness.domain.errors import ErrorCode, HarnessError, PauseRequested, require
from agent_harness.domain.model import RunOptions
from agent_harness.domain.states import TaskStatus
from agent_harness.ports.state import Clock, IdGenerator, StateStore


@dataclass(frozen=True)
class DelegatedTask:
    alias: str
    idempotency_key: str
    agent_definition_id: str
    agent_version: str
    objective: str
    acceptance_criteria: JsonObject
    input_artifact_ids: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    max_steps: int | None = None

    def __post_init__(self) -> None:
        require(
            all(
                isinstance(value, str) and value.strip()
                for value in (
                    self.alias,
                    self.idempotency_key,
                    self.agent_definition_id,
                    self.agent_version,
                    self.objective,
                )
            ),
            "Delegated task identity and objective are required",
        )
        require(
            len(self.alias) <= 64 and len(self.idempotency_key) <= 200,
            "Delegation key too long",
        )
        require(
            len(set(self.dependencies)) == len(self.dependencies)
            and len(set(self.capabilities)) == len(self.capabilities),
            "Delegation contains duplicate references",
        )
        require(len(self.input_artifact_ids) <= 16, "Too many input artifacts")
        require(
            self.max_steps is None or 1 <= self.max_steps <= 100,
            "Requested child step limit must be 1..100",
        )


class TaskCoordinator:
    MAX_CHILDREN_PER_RUN = 16

    def __init__(
        self,
        store: StateStore,
        clock: Clock,
        ids: IdGenerator,
        factory: AgentFactory,
        scheduler: TaskScheduler,
    ) -> None:
        self.store, self.clock, self.ids = store, clock, ids
        self.factory, self.scheduler = factory, scheduler

    @staticmethod
    def _order(specs: tuple[DelegatedTask, ...]) -> tuple[DelegatedTask, ...]:
        require(1 <= len(specs) <= 8, "Delegation must contain 1..8 child tasks")
        aliases = {spec.alias for spec in specs}
        require(len(aliases) == len(specs), "Delegation aliases must be unique")
        require(
            all(dependency in aliases for spec in specs for dependency in spec.dependencies),
            "Batch dependencies must reference a task in the same delegation",
        )
        remaining = list(specs)
        resolved: set[str] = set()
        ordered: list[DelegatedTask] = []
        while remaining:
            ready = next((spec for spec in remaining if set(spec.dependencies) <= resolved), None)
            if ready is None:
                raise HarnessError(
                    ErrorCode.VALIDATION,
                    "Delegated task dependencies contain a cycle",
                )
            ordered.append(ready)
            remaining.remove(ready)
            resolved.add(ready.alias)
        return tuple(ordered)

    async def _create_children(
        self,
        user_id: str,
        parent_task_id: str,
        specs: tuple[DelegatedTask, ...],
        options: RunOptions,
    ) -> tuple[Task, ...]:
        ordered = self._order(specs)
        async with self.store.transaction(user_id) as tx:
            parent = await tx.get_task(parent_task_id)
            if parent is None:
                raise HarnessError(ErrorCode.AUTHORIZATION, "Parent task is unavailable")
            parent_instance = self.factory.create(parent, options)
            definition = parent_instance.definition
            require(definition.may_delegate, "This agent definition cannot delegate")
            require(parent.depth < definition.max_depth, "Maximum delegation depth exceeded")
            existing_children = tuple(
                task
                for task in await tx.tasks(parent.run_id)
                if task.parent_task_id == parent_task_id
            )
            all_run_tasks = await tx.tasks(parent.run_id)
            existing_total = sum(task.parent_task_id is not None for task in all_run_tasks)
            new_count = 0
            for spec in ordered:
                if await tx.delegated_task(parent_task_id, spec.idempotency_key) is None:
                    new_count += 1
            require(
                len(existing_children) + new_count <= definition.max_children,
                "Maximum child count exceeded",
            )
            require(
                existing_total + new_count <= self.MAX_CHILDREN_PER_RUN,
                "Maximum total child count exceeded",
            )
            task_ids = {
                spec.alias: str(uuid5(UUID(parent_task_id), spec.idempotency_key))
                for spec in ordered
            }
            children: list[Task] = []
            now = self.clock.utc_now()
            for spec in ordered:
                child_definition = self.factory.registry.get(
                    spec.agent_definition_id, spec.agent_version
                )
                require(
                    child_definition.definition_id != "coordinator"
                    or parent.depth + 1 < definition.max_depth,
                    "Delegation depth limit prevents another coordinator",
                )
                requested_capabilities = (
                    set(spec.capabilities)
                    if spec.capabilities
                    else set(child_definition.capabilities) & set(parent_instance.capabilities)
                )
                require(
                    requested_capabilities <= set(child_definition.capabilities)
                    and requested_capabilities <= set(parent_instance.capabilities),
                    "Child capabilities must narrow both agent and parent permissions",
                )
                requested_steps = spec.max_steps or parent_instance.max_steps
                require(
                    requested_steps <= parent_instance.max_steps,
                    "Child step limit cannot exceed the parent limit",
                )
                dependencies = tuple(task_ids[value] for value in spec.dependencies)
                existing_artifacts = {
                    item.artifact_id
                    for item in await tx.artifacts(parent.run_id)
                    if item.status == "committed"
                }
                require(
                    set(spec.input_artifact_ids) <= existing_artifacts,
                    "Delegation references an unavailable artifact",
                )
                child_id = task_ids[spec.alias]
                task = Task(
                    child_id,
                    parent.run_id,
                    user_id,
                    spec.objective,
                    now,
                    now,
                    acceptance_criteria=spec.acceptance_criteria,
                    parent_task_id=parent_task_id,
                    dependencies=dependencies,
                    agent_instance_id=self.ids.new_id(),
                    deadline=parent.deadline,
                    agent_definition_id=child_definition.definition_id,
                    agent_definition_version=child_definition.version,
                    depth=parent.depth + 1,
                    required=True,
                    capabilities=tuple(sorted(requested_capabilities)),
                    handoff=JsonObject(
                        json.dumps(
                            {
                                "max_steps": requested_steps,
                                "input_artifact_ids": list(spec.input_artifact_ids),
                            }
                        )
                    ),
                    idempotency_key=spec.idempotency_key,
                )
                prior = await tx.delegated_task(parent_task_id, spec.idempotency_key)
                if prior is not None:
                    require(
                        prior.task_id == child_id
                        and prior.objective == task.objective
                        and prior.agent_definition_id == task.agent_definition_id
                        and prior.agent_definition_version == task.agent_definition_version
                        and prior.dependencies == task.dependencies
                        and prior.capabilities == task.capabilities,
                        "Idempotency key was reused for a different delegation",
                    )
                    require(
                        prior.acceptance_criteria == task.acceptance_criteria
                        and prior.handoff == task.handoff,
                        "Idempotency key was reused for a different delegation",
                    )
                    task = prior
                else:
                    await tx.create_task(task)
                    await tx.save_delegation(parent_task_id, spec.idempotency_key, task)
                children.append(task)
            if parent.status == TaskStatus.RUNNING:
                await tx.transition_task(
                    parent_task_id, parent.version, TaskStatus.WAITING_CHILDREN
                )
            elif parent.status != TaskStatus.WAITING_CHILDREN:
                raise HarnessError(
                    ErrorCode.CONCURRENCY_CONFLICT,
                    "Only a running parent task may accept delegated work",
                    retryable=True,
                )
            for child in children:
                if not child.dependencies and child.status == TaskStatus.PENDING:
                    await tx.transition_task(child.task_id, child.version, TaskStatus.READY)
            return tuple(children)

    async def delegate_and_run(
        self,
        user_id: str,
        parent_task_id: str,
        specs: tuple[DelegatedTask, ...],
        options: RunOptions,
        execute_task: Callable[[Task], Awaitable[Task]],
    ) -> JsonObject:
        children = await self._create_children(user_id, parent_task_id, specs, options)
        aliases = tuple(spec.alias for spec in specs)
        task_ids = {
            spec.alias: str(uuid5(UUID(parent_task_id), spec.idempotency_key)) for spec in specs
        }
        all_children = list(children)
        while True:
            async with self.store.transaction(user_id) as tx:
                run_tasks = await tx.tasks(children[0].run_id)
                refreshed = {task.task_id: task for task in run_tasks}
                failed_outcome = False
                for child in all_children:
                    current = refreshed[child.task_id]
                    if current.status == TaskStatus.PENDING:
                        dependencies = [refreshed[dep] for dep in current.dependencies]
                        if all(dep.status == TaskStatus.SUCCEEDED for dep in dependencies):
                            current = await tx.transition_task(
                                current.task_id, current.version, TaskStatus.READY
                            )
                            refreshed[current.task_id] = current
                    if current.status == TaskStatus.READY and current.dependencies:
                        dependency_results: list[dict[str, Any]] = []
                        for dependency_id in current.dependencies:
                            dependency = refreshed[dependency_id]
                            dependency_artifacts: list[dict[str, Any]] = [
                                {
                                    "artifact_id": artifact.artifact_id,
                                    "name": artifact.name,
                                    "version": artifact.version,
                                    "checksum": artifact.checksum,
                                }
                                for artifact in await tx.artifacts(dependency.run_id)
                                if artifact.task_id == dependency.task_id
                                and artifact.status == "committed"
                            ]
                            dependency_results.append(
                                {
                                    "task_id": dependency.task_id,
                                    "status": dependency.status.value,
                                    "result": json.loads(dependency.result.text)
                                    if dependency.result
                                    else None,
                                    "error": json.loads(dependency.error.text)
                                    if dependency.error
                                    else None,
                                    "artifacts": dependency_artifacts,
                                }
                            )
                        handoff: dict[str, Any] = json.loads(current.handoff.text)
                        handoff["dependency_results"] = dependency_results
                        raw_input_ids = handoff.get("input_artifact_ids", [])
                        require(
                            isinstance(raw_input_ids, list)
                            and all(isinstance(item, str) for item in raw_input_ids),
                            "Task handoff input_artifact_ids must be a list of strings",
                        )
                        input_ids = set(raw_input_ids)
                        input_ids.update(
                            artifact["artifact_id"]
                            for dependency in dependency_results
                            for artifact in dependency["artifacts"]
                        )
                        handoff["input_artifact_ids"] = sorted(input_ids)
                        current = await tx.set_task_handoff(
                            current.task_id,
                            current.version,
                            JsonObject(json.dumps(handoff)),
                        )
                        refreshed[current.task_id] = current
                ready = tuple(
                    refreshed[child.task_id]
                    for child in all_children
                    if refreshed[child.task_id].status == TaskStatus.READY
                )
                pending = tuple(
                    refreshed[child.task_id]
                    for child in all_children
                    if refreshed[child.task_id].status
                    in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}
                )
                failed = tuple(
                    refreshed[child.task_id]
                    for child in all_children
                    if refreshed[child.task_id].status
                    in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED}
                )
                if failed:
                    if options.partial_result_policy != "allow":
                        failed_outcome = True
                        for item in pending:
                            if item.status in {TaskStatus.PENDING, TaskStatus.READY}:
                                await tx.transition_task(
                                    item.task_id, item.version, TaskStatus.BLOCKED
                                )
                        pending = ()
                    else:
                        made_progress = True
                        while made_progress:
                            made_progress = False
                            for item in all_children:
                                current = refreshed[item.task_id]
                                if current.status != TaskStatus.PENDING:
                                    continue
                                dependencies = [
                                    refreshed[dependency_id]
                                    for dependency_id in current.dependencies
                                ]
                                if any(
                                    dependency.status
                                    in {
                                        TaskStatus.FAILED,
                                        TaskStatus.CANCELLED,
                                        TaskStatus.BLOCKED,
                                    }
                                    for dependency in dependencies
                                ):
                                    refreshed[item.task_id] = await tx.transition_task(
                                        current.task_id,
                                        current.version,
                                        TaskStatus.BLOCKED,
                                    )
                                    made_progress = True
                        ready = tuple(
                            refreshed[child.task_id]
                            for child in all_children
                            if refreshed[child.task_id].status == TaskStatus.READY
                        )
                        pending = tuple(
                            refreshed[child.task_id]
                            for child in all_children
                            if refreshed[child.task_id].status
                            in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING}
                        )
                if not pending:
                    break
            if failed_outcome:
                break
            if not ready:
                raise HarnessError(
                    ErrorCode.CONCURRENCY_CONFLICT,
                    "No child task is ready although delegated tasks remain",
                    retryable=True,
                )
            parent = await self._parent(user_id, parent_task_id)
            definition = self.factory.registry.get(
                parent.agent_definition_id, parent.agent_definition_version
            )
            limit = min(definition.max_active_children, self.scheduler.concurrency)
            await self.scheduler.run(ready, execute_task, active_limit=limit)
            async with self.store.transaction(user_id) as tx:
                refreshed_after_run = await tx.tasks(children[0].run_id)
            if any(child.status == TaskStatus.PAUSED for child in refreshed_after_run):
                raise PauseRequested()
        async with self.store.transaction(user_id) as tx:
            current_parent = await tx.get_task(parent_task_id)
            assert current_parent is not None
            if current_parent.status == TaskStatus.WAITING_CHILDREN:
                current_parent = await tx.transition_task(
                    parent_task_id, current_parent.version, TaskStatus.READY
                )
                current_parent = await tx.transition_task(
                    parent_task_id, current_parent.version, TaskStatus.RUNNING
                )
            results = []
            for alias in aliases:
                task_id = task_ids[alias]
                task = await tx.get_task(task_id)
                assert task is not None
                artifacts = [
                    {
                        "artifact_id": artifact.artifact_id,
                        "name": artifact.name,
                        "version": artifact.version,
                        "checksum": artifact.checksum,
                    }
                    for artifact in await tx.artifacts(task.run_id)
                    if artifact.task_id == task.task_id and artifact.status == "committed"
                ]
                results.append(
                    {
                        "alias": alias,
                        "task_id": task.task_id,
                        "status": task.status.value,
                        "result": json.loads(task.result.text) if task.result else None,
                        "error": json.loads(task.error.text) if task.error else None,
                        "artifacts": artifacts,
                    }
                )
            return JsonObject(json.dumps({"children": results}))

    async def _parent(self, user_id: str, parent_task_id: str) -> Task:
        async with self.store.transaction(user_id) as tx:
            parent = await tx.get_task(parent_task_id)
        if parent is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Parent task is unavailable")
        return parent
