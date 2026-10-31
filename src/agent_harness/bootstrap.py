"""Explicit lifecycle wiring shared by CLI and local demonstrations."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from agent_harness.adapters.fake import offline_model
from agent_harness.adapters.ollama import OllamaClient
from agent_harness.adapters.system import SystemClock, UuidGenerator
from agent_harness.agents.runner import AgentRunner
from agent_harness.application.runs import RunService
from agent_harness.artifacts.service import ArtifactService
from agent_harness.config import Settings
from agent_harness.domain.model import RunOptions
from agent_harness.models.gateway import ModelGateway
from agent_harness.persistence.sqlite import SQLiteStore
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
    store: SQLiteStore
    runs: RunService
    clock: SystemClock
    ids: UuidGenerator
    scheduler: ResourceScheduler
    blocking_tools: BlockingToolAdapter


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
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    clock, ids = SystemClock(), UuidGenerator()
    scheduler = resource_scheduler(settings)
    blocking_tools = BlockingToolAdapter(
        settings.blocking_tool_workers, settings.blocking_tool_queue_capacity
    )
    async with SQLiteStore(settings.database, clock, ids) as store:
        try:
            yield Application(
                store,
                RunService(store, clock, ids),
                clock,
                ids,
                scheduler,
                blocking_tools,
            )
        finally:
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
        tools = ToolExecutor(
            app.store,
            builtin_registry(
                ArtifactService(app.store, Path(options.artifact_root)),
                Path(options.workspace),
                Path(options.artifact_root),
            ),
            app.clock,
            app.scheduler,
            app.blocking_tools,
        )
        yield AgentRunner(
            app.store,
            ModelGateway(client, app.scheduler, options.provider),
            app.clock,
            app.ids,
            tools,
        )
