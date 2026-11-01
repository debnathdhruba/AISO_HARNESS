"""Application service for a preplanned, syscall-free agent_orchestration_core run."""

import json
from collections.abc import Awaitable, Callable

from agent_harness.agent_orchestration_core.automatic import AutomaticPlan, decompose
from agent_harness.agent_orchestration_core.paging import PageFrame
from agent_harness.agent_orchestration_core.runtime import KernelPlanRuntime
from agent_harness.application.coordinator import TaskCoordinator
from agent_harness.domain.entities import JsonObject, Run, Task
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.kernel import ExecutionPlan
from agent_harness.domain.model import RunOptions
from agent_harness.domain.states import RunStatus, TaskStatus
from agent_harness.protocols.state import StateStore


class AgentOrchestrationCoreService:
    """Owns root state transitions while the kernel dispatches its child DAG."""

    def __init__(self, store: StateStore, coordinator: TaskCoordinator) -> None:
        self._store = store
        self._runtime = KernelPlanRuntime(coordinator)

    async def recover_running_task(
        self,
        user_id: str,
        run_id: str,
        task_id: str,
        objective: str,
        context: str,
        options: RunOptions,
        execute_task: Callable[[Task], Awaitable[Task]],
        automatic_plan: AutomaticPlan | None = None,
    ) -> Task:
        """Run bounded workers for a live task and persist their reduction frame."""
        async with self._store.transaction(user_id) as tx:
            run = await tx.get_run(run_id)
            task = await tx.get_task(task_id)
            require(run is not None, "Orchestration run is unavailable")
            require(task is not None, "Orchestration task is unavailable")
            assert run is not None and task is not None
            require(run.status == RunStatus.RUNNING, "Orchestration requires a running run")
            require(task.status == TaskStatus.RUNNING, "Orchestration requires a running task")
            existing = json.loads(task.handoff.text)
            require(
                "agent_orchestration_core_recovery" not in existing,
                "Task already used context recovery",
            )

        automatic = automatic_plan or decompose(context, options, objective=objective, force=True)
        execution = await self._runtime.execute(
            user_id,
            task_id,
            automatic.plan,
            automatic.pages,
            options,
            execute_task,
        )
        async with self._store.transaction(user_id) as tx:
            task = await tx.get_task(task_id)
            require(
                task is not None and task.status == TaskStatus.RUNNING,
                "Orchestration parent stopped while workers were running",
            )
            assert task is not None
            handoff = json.loads(task.handoff.text)
            handoff["agent_orchestration_core_execution"] = json.loads(execution.document().text)
            handoff["agent_orchestration_core_reduction"] = {
                "automatic": True,
                "objective": automatic.objective,
            }
            handoff["agent_orchestration_core_recovery"] = {
                "plan_id": automatic.plan.plan_id,
                "page_count": len(automatic.pages),
                "worker_count": len(automatic.plan.tasks),
            }
            updated = await tx.set_task_handoff(
                task.task_id, task.version, JsonObject(json.dumps(handoff))
            )
            return updated

    async def execute(
        self,
        user_id: str,
        run_id: str,
        plan: ExecutionPlan,
        pages: tuple[PageFrame, ...],
        execute_task: Callable[[Task], Awaitable[Task]],
        finalize_root: Callable[[str, str, str], Awaitable[Run]],
        reduction_context: JsonObject | None = None,
    ) -> Run:
        """Run plan leaves first, then invoke the existing root reducer exactly once."""
        async with self._store.transaction(user_id) as tx:
            run = await tx.get_run(run_id)
            if run is None:
                raise HarnessError(ErrorCode.AUTHORIZATION, "Run is unavailable to this principal")
            require(
                run.status == RunStatus.QUEUED,
                "Agent Orchestration Core runs must start from queued state",
            )
            root_id = str(json.loads(run.config.policy.text)["root_task_id"])
            root = await tx.get_task(root_id)
            require(root is not None and root.status == TaskStatus.READY, "Root task is not ready")
            assert root is not None
            options = RunOptions.restore(run.config.model)
            run = await tx.transition_run(run_id, run.version, RunStatus.RUNNING)
            root = await tx.transition_task(root.task_id, root.version, TaskStatus.RUNNING)

        execution = await self._runtime.execute(
            user_id, root_id, plan, pages, options, execute_task
        )
        async with self._store.transaction(user_id) as tx:
            root = await tx.get_task(root_id)
            require(
                root is not None and root.status == TaskStatus.RUNNING,
                "Root reducer is unavailable",
            )
            assert root is not None
            handoff = json.loads(root.handoff.text)
            handoff["agent_orchestration_core_execution"] = json.loads(execution.document().text)
            if reduction_context is not None:
                handoff["agent_orchestration_core_reduction"] = json.loads(reduction_context.text)
            await tx.set_task_handoff(root.task_id, root.version, JsonObject(json.dumps(handoff)))
        return await finalize_root(user_id, run_id, root_id)
