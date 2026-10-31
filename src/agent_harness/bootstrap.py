"""Explicit lifecycle wiring shared by CLI and local demonstrations."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import json
from pathlib import Path

from agent_harness.adapters.fake import offline_model
from agent_harness.adapters.ollama import OllamaClient
from agent_harness.adapters.system import SystemClock, UuidGenerator
from agent_harness.agents.factory import AgentFactory, local_agent_registry
from agent_harness.agents.runner import AgentRunner
from agent_harness.agents.scheduler import TaskScheduler
from agent_harness.application.coordinator import DelegatedTask, TaskCoordinator
from agent_harness.application.runs import RunService
from agent_harness.artifacts.service import ArtifactService
from agent_harness.config import Settings
from agent_harness.domain.entities import JsonObject, Task
from agent_harness.domain.errors import require
from agent_harness.domain.tooling import Invocation
from agent_harness.domain.model import RunOptions
from agent_harness.models.gateway import ModelGateway
from agent_harness.persistence.mongo import MongoStore
from agent_harness.ports.model import ModelClient
from agent_harness.scheduling import (
    BlockingToolAdapter,
    ResourceKind,
    ResourceLimit,
    ResourceScheduler,
)
from agent_harness.tools.builtins import builtin_registry
from agent_harness.tools.executor import ToolExecutor


@dataclass(frozen=True)
class Application:
    store: MongoStore
    runs: RunService
    clock: SystemClock
    ids: UuidGenerator
    scheduler: ResourceScheduler
    blocking_tools: BlockingToolAdapter
    task_scheduler: TaskScheduler
    agent_factory: AgentFactory
    coordinator: TaskCoordinator


def resource_scheduler(settings: Settings) -> ResourceScheduler:
    return ResourceScheduler(
        {
            ResourceKind.MODEL: ResourceLimit(
                settings.model_concurrency,
                settings.model_queue_capacity,
                settings.model_provider_concurrency,
            ),
            ResourceKind.TOOL: ResourceLimit(
                settings.tool_concurrency, settings.tool_queue_capacity
            ),
            ResourceKind.MEMORY: ResourceLimit(
                settings.memory_concurrency, settings.memory_queue_capacity
            ),
            ResourceKind.ARTIFACT: ResourceLimit(
                settings.artifact_concurrency, settings.artifact_queue_capacity, 1
            ),
        }
    )


@asynccontextmanager
async def application(settings: Settings) -> AsyncIterator[Application]:
    clock, ids = SystemClock(), UuidGenerator()
    scheduler = resource_scheduler(settings)
    task_scheduler = TaskScheduler(settings.task_concurrency, settings.task_queue_capacity)
    agent_factory = AgentFactory(local_agent_registry())
    blocking_tools = BlockingToolAdapter(
        settings.blocking_tool_workers, settings.blocking_tool_queue_capacity
    )
    async with MongoStore(
        settings.mongodb_uri.get_secret_value(),
        settings.database_name,
        clock,
        ids,
    ) as store:
        try:
            yield Application(
                store,
                RunService(store, clock, ids),
                clock,
                ids,
                scheduler,
                blocking_tools,
                task_scheduler,
                agent_factory,
                TaskCoordinator(store, clock, ids, agent_factory, task_scheduler),
            )
        finally:
            await task_scheduler.shutdown()
            await scheduler.shutdown(cancel_running=True)
            await blocking_tools.shutdown(wait=True)


@asynccontextmanager
async def runner(
    app: Application, settings: Settings, options: RunOptions
) -> AsyncIterator[AgentRunner]:
    async with AsyncExitStack() as stack:
        client: ModelClient
        if options.provider == "fake":
            client = offline_model()
        else:
            ollama = await stack.enter_async_context(OllamaClient(base_url=settings.ollama_url))
            client = ollama
        agent_holder: dict[str, AgentRunner] = {}

        async def delegate(invocation: Invocation) -> JsonObject:
            require(
                len(invocation.arguments.text.encode("utf-8")) <= 16_384,
                "Delegation request exceeds size limit",
            )
            args = json.loads(invocation.arguments.text)
            specs = tuple(
                DelegatedTask(
                    item["alias"],
                    item["idempotency_key"],
                    item["agent_definition_id"],
                    item.get("agent_version", "1"),
                    item["objective"],
                    JsonObject(json.dumps(item.get("acceptance_criteria", {}))),
                    tuple(item.get("input_artifact_ids", ())),
                    tuple(item.get("dependencies", ())),
                    tuple(item.get("capabilities", ())),
                    item.get("max_steps"),
                )
                for item in args["tasks"]
            )
            agent = agent_holder["agent"]

            async def execute_child(task: Task) -> Task:
                await agent.execute_task(invocation.user_id, invocation.run_id, task.task_id)
                async with app.store.transaction(invocation.user_id) as tx:
                    result = await tx.get_task(task.task_id)
                if result is None:
                    raise RuntimeError("Delegated task disappeared during execution")
                return result

            return await app.coordinator.delegate_and_run(
                invocation.user_id,
                invocation.task_id,
                specs,
                options,
                execute_child,
            )

        tools = ToolExecutor(
            app.store,
            builtin_registry(
                ArtifactService(app.store, Path(options.artifact_root)),
                Path(options.workspace),
                Path(options.artifact_root),
                delegate,
            ),
            app.clock,
            app.scheduler,
            app.blocking_tools,
        )
        agent = AgentRunner(
            app.store,
            ModelGateway(client, app.scheduler, options.provider),
            app.clock,
            app.ids,
            tools,
            app.agent_factory,
        )
        agent_holder["agent"] = agent
        yield agent
