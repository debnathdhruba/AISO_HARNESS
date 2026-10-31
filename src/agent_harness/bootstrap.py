"""Explicit lifecycle wiring shared by CLI and local demonstrations."""

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
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
from agent_harness.domain.memory import MemorySharing
from agent_harness.domain.model import RunOptions
from agent_harness.domain.tooling import Invocation
from agent_harness.memory import MemoryService
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
    memory: MemoryService


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
async def application(
    settings: Settings, *, own_scheduler: bool = False
) -> AsyncIterator[Application]:
    clock, ids = SystemClock(), UuidGenerator()
    scheduler = resource_scheduler(settings)
    task_scheduler = TaskScheduler(settings.task_concurrency, settings.task_queue_capacity)
    agent_factory = AgentFactory(local_agent_registry())
    blocking_tools = BlockingToolAdapter(
        settings.blocking_tool_workers, settings.blocking_tool_queue_capacity
    )
    store = MongoStore(
        settings.mongodb_uri.get_secret_value(),
        settings.database_name,
        clock,
        ids,
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(store)
        if own_scheduler:
            await stack.enter_async_context(store.scheduler_ownership())
        try:
            memory = MemoryService(store, clock, ids, scheduler=scheduler)
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
                memory,
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

        async def save_memory(invocation: Invocation) -> JsonObject:
            args = json.loads(invocation.arguments.text)
            async with app.store.transaction(invocation.user_id) as tx:
                task = await tx.get_task(invocation.task_id)
            require(task is not None, "Memory source task is unavailable")
            assert task is not None
            record = await app.memory.create(
                invocation.user_id,
                task.agent_definition_id,
                args["content"],
                tags=tuple(args.get("tags", ())),
                sharing=MemorySharing(args["sharing"]),
                source_run_id=invocation.run_id,
                source_task_id=invocation.task_id,
                record_id=invocation.invocation_id,
            )
            return JsonObject(
                json.dumps(
                    {
                        "record_id": record.record_id,
                        "sharing": record.sharing.value,
                        "tags": list(record.tags),
                        "source_run_id": record.source_run_id,
                        "source_task_id": record.source_task_id,
                    }
                )
            )

        tools = ToolExecutor(
            app.store,
            builtin_registry(
                ArtifactService(app.store, Path(options.artifact_root)),
                Path(options.workspace),
                Path(options.artifact_root),
                delegate,
                memory_save=save_memory if options.memory_extraction_enabled else None,
            ),
            app.clock,
            app.scheduler,
            app.blocking_tools,
            app.store.assert_scheduler_ownership,
        )

        agent = AgentRunner(
            app.store,
            ModelGateway(client, app.scheduler, options.provider),
            app.clock,
            app.ids,
            tools,
            app.agent_factory,
            app.store.assert_scheduler_ownership,
            app.memory,
        )
        agent_holder["agent"] = agent
        yield agent
