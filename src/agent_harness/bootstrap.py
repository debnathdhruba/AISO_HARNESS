"""Explicit lifecycle wiring shared by CLI and local demonstrations."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from agent_harness.adapters.fake import offline_model
from agent_harness.adapters.ollama import OllamaClient
from agent_harness.adapters.system import SystemClock, UuidGenerator
from agent_harness.agents.runner import AgentRunner
from agent_harness.application.runs import RunService
from agent_harness.config import Settings
from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import RunOptions
from agent_harness.models.gateway import ModelGateway
from agent_harness.persistence.sqlite import SQLiteStore
from agent_harness.ports.model import ModelClient


@dataclass(frozen=True)
class Application:
    store: SQLiteStore
    runs: RunService
    clock: SystemClock
    ids: UuidGenerator


@asynccontextmanager
async def application(settings: Settings) -> AsyncIterator[Application]:
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    clock, ids = SystemClock(), UuidGenerator()
    async with SQLiteStore(settings.database, clock, ids) as store:
        yield Application(store, RunService(store, clock, ids), clock, ids)


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
            try:
                async with asyncio.timeout(min(options.timeout_seconds, 30)):
                    await ollama.discover((options.decision_model, options.response_model))
            except TimeoutError:
                raise HarnessError(
                    ErrorCode.DEADLINE_EXCEEDED, "Ollama model discovery timed out"
                ) from None
            client = ollama
        yield AgentRunner(app.store, ModelGateway(client), app.clock, app.ids)
