"""Kernel-owned execution of an already validated subagent DAG."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent_harness.agent_orchestration_core.paging import PageFrame
from agent_harness.application.coordinator import DelegatedTask, TaskCoordinator
from agent_harness.domain.entities import JsonObject, Task
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.kernel import ExecutionPlan, ResultEnvelope
from agent_harness.domain.model import RunOptions
from agent_harness.domain.states import TaskStatus
from agent_harness.memory.short_term import KernelMemory


@dataclass(frozen=True)
class KernelExecution:
    """The durable child results exposed to a reducer, not mutable worker state."""

    plan: ExecutionPlan
    results: tuple[ResultEnvelope, ...]

    def document(self) -> JsonObject:
        return JsonObject(
            json.dumps(
                {
                    "plan": json.loads(self.plan.document().text),
                    "results": [
                        {
                            "alias": item.alias,
                            "task_id": item.task_id,
                            "attempt": item.attempt,
                            "status": item.status,
                            "schema": item.schema,
                            "output": json.loads(item.output.text),
                            "warnings": list(item.warnings),
                        }
                        for item in self.results
                    ],
                }
            )
        )


class KernelPlanRuntime:
    """Schedules a fixed DAG; agents only receive frames and return task results."""

    def __init__(self, coordinator: TaskCoordinator) -> None:
        self._coordinator = coordinator

    @staticmethod
    def specs(
        plan: ExecutionPlan, pages: tuple[PageFrame, ...], max_input_bytes: int = 16_384
    ) -> tuple[DelegatedTask, ...]:
        page_map = {page.page_id: page for page in pages}
        specs: list[DelegatedTask] = []
        for task in plan.ordered():
            selected = tuple(page_map[page_id] for page_id in task.input_pages)
            require(
                sum(len(page.text.encode("utf-8")) for page in selected) <= max_input_bytes // 2,
                "Plan task pages exceed its bounded input frame",
            )
            acceptance = json.loads(task.acceptance_criteria.text)
            if json.loads(task.result_schema.text):
                acceptance["result_schema"] = json.loads(task.result_schema.text)
            handoff = JsonObject(
                json.dumps(
                    {
                        "kernel": {
                            "plan_id": plan.plan_id,
                            "task_alias": task.alias,
                            "result_schema": json.loads(task.result_schema.text),
                            "managed_dependencies": True,
                        },
                        "memory_keys": [f"page:{page.page_id}" for page in selected],
                    }
                )
            )
            specs.append(
                DelegatedTask(
                    alias=task.alias,
                    idempotency_key=f"kernel:{plan.plan_id}:{task.alias}",
                    agent_definition_id=task.agent_definition_id,
                    agent_version=task.agent_version,
                    objective=task.objective,
                    acceptance_criteria=JsonObject(json.dumps(acceptance)),
                    dependencies=task.depends_on,
                    capabilities=task.capabilities,
                    max_steps=task.max_steps,
                    handoff=handoff,
                    enforce_capabilities=True,
                )
            )
        return tuple(specs)

    async def execute(
        self,
        user_id: str,
        parent_task_id: str,
        plan: ExecutionPlan,
        pages: tuple[PageFrame, ...],
        options: RunOptions,
        execute_task: Callable[[Task], Awaitable[Task]],
    ) -> KernelExecution:
        """Execute independent plan nodes concurrently through the existing fair scheduler."""
        require(
            {page.page_id for page in pages}
            >= {page_id for task in plan.tasks for page_id in task.input_pages},
            "Kernel plan refers to unavailable pages",
        )
        memory = KernelMemory()
        readers_by_page = {
            page.page_id: tuple(
                task.alias for task in plan.tasks if page.page_id in task.input_pages
            )
            for page in pages
        }
        for page in pages:
            readers = readers_by_page[page.page_id]
            if readers:
                await memory.put(
                    f"page:{page.page_id}",
                    JsonObject(
                        json.dumps(
                            {
                                "page_id": page.page_id,
                                "byte_start": page.byte_start,
                                "byte_end": page.byte_end,
                                "text": page.text,
                            }
                        )
                    ),
                    readers=readers,
                )
        dependents = {
            task.alias: tuple(child.alias for child in plan.tasks if task.alias in child.depends_on)
            for task in plan.tasks
        }

        async def dispatch(task: Task) -> Task:
            handoff = json.loads(task.handoff.text)
            kernel = handoff["kernel"]
            alias = kernel["task_alias"]
            require(isinstance(alias, str), "Kernel task alias is invalid")
            keys = handoff.get("memory_keys", [])
            require(
                isinstance(keys, list) and all(isinstance(key, str) for key in keys),
                "Kernel memory keys are invalid",
            )
            pages_for_task = [json.loads((await memory.read(alias, key)).text) for key in keys]
            dependencies = next(item.depends_on for item in plan.tasks if item.alias == alias)
            dependency_results = [
                json.loads((await memory.read(alias, f"result:{dependency}")).text)
                for dependency in dependencies
            ]
            handoff["pages"] = pages_for_task
            handoff["dependency_results"] = dependency_results
            async with self._coordinator.store.transaction(task.user_id) as tx:
                current = await tx.get_task(task.task_id)
                require(
                    current is not None and current.status == TaskStatus.READY,
                    "Kernel task is not ready",
                )
                assert current is not None
                await tx.set_task_handoff(
                    current.task_id, current.version, JsonObject(json.dumps(handoff))
                )
            completed = await execute_task(task)
            result = completed.result if completed.result is not None else JsonObject()
            if dependents[alias]:
                await memory.put(
                    f"result:{alias}",
                    JsonObject(
                        json.dumps(
                            {
                                "task_id": completed.task_id,
                                "status": completed.status.value,
                                "result": json.loads(result.text),
                                "error": json.loads(completed.error.text)
                                if completed.error is not None
                                else None,
                            }
                        )
                    ),
                    readers=dependents[alias],
                )
            return completed

        raw = await self._coordinator.delegate_and_run(
            user_id,
            parent_task_id,
            self.specs(plan, pages, options.max_input_bytes),
            options,
            dispatch,
        )
        document = json.loads(raw.text)
        by_alias = {item["alias"]: item for item in document["children"]}
        results: list[ResultEnvelope] = []
        for task in plan.ordered():
            child = by_alias.get(task.alias)
            if child is None:
                raise HarnessError(ErrorCode.STORAGE, "Kernel result is missing a planned task")
            status = str(child["status"])
            output = child["result"] if isinstance(child.get("result"), dict) else {}
            warnings: tuple[str, ...] = ()
            if child.get("error"):
                warnings = ("task did not produce a successful result",)
            results.append(
                ResultEnvelope(
                    task.alias,
                    str(child["task_id"]),
                    1,
                    {
                        TaskStatus.SUCCEEDED.value: "succeeded",
                        TaskStatus.FAILED.value: "failed",
                        TaskStatus.CANCELLED.value: "cancelled",
                        TaskStatus.BLOCKED.value: "blocked",
                    }.get(status, "blocked"),
                    f"{task.alias}.v1",
                    JsonObject(json.dumps(output)),
                    warnings,
                )
            )
        return KernelExecution(plan, tuple(results))
