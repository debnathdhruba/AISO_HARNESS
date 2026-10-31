import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import FixedClock, SequenceIds
from typer.testing import CliRunner

from agent_harness.adapters.fake import ScriptedModel
from agent_harness.agents.runner import AgentRunner
from agent_harness.application.runs import RunService
from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import (
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunOptions,
    Stage,
    StepStatus,
    ToolCall,
)
from agent_harness.domain.states import RunStatus
from agent_harness.interfaces.cli import app
from agent_harness.models.context import build_context, size
from agent_harness.models.gateway import ModelGateway
from agent_harness.persistence.sqlite import SQLiteStore
from agent_harness.ports.state import StateTransaction


def answer(text: str = "Final answer") -> ModelResponse:
    return ModelResponse(Message(Role.ASSISTANT, text), FinishReason.STOP)


@pytest.mark.asyncio
async def test_two_models_ordered_messages_and_inspect(store: SQLiteStore) -> None:
    clock, ids = FixedClock(), SequenceIds()
    run = await RunService(store, clock, ids).submit(
        "alice",
        "Explain a queue",
        RunOptions(
            routing_mode="two_stage",
        ),
    )
    model = ScriptedModel((answer("Draft"), answer("A queue keeps waiting items.")))
    result = await AgentRunner(store, ModelGateway(model), clock, ids).execute("alice", run.run_id)
    assert result.status == RunStatus.COMPLETED
    assert result.result is not None and json.loads(result.result.text)["verified"] is False
    assert [request.model for request in model.requests] == ["lfm2.5-thinking:1.2b", "gemma3:1b"]
    assert [m.role for m in model.requests[1].messages] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
    ]
    assert model.requests[1].messages[2].content == "Draft"
    async with store.transaction("alice") as tx:
        steps = await tx.steps(run.run_id)
        assert [step.request for step in steps] == model.requests
        assert all(step.status == StepStatus.APPLIED for step in steps)
        assert all(step.response.usage.input_tokens is None for step in steps)
    async with store.transaction("bob") as tx:
        assert await tx.steps(run.run_id) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        answer(""),
        ModelResponse(Message(Role.ASSISTANT, "truncated"), FinishReason.LENGTH),
        ModelResponse(Message(Role.ASSISTANT, "unsupported"), FinishReason.UNSUPPORTED),
        ModelResponse(
            Message(
                Role.ASSISTANT,
                "Do not treat this as final",
                (
                    ToolCall("call-1", "calculator", JsonObject('{"a":1}')),
                    ToolCall("call-2", "calculator", JsonObject('{"a":2}')),
                ),
            ),
            FinishReason.STOP,
        ),
    ],
)
async def test_invalid_or_action_response_never_completes(
    store: SQLiteStore, response: ModelResponse
) -> None:
    clock, ids = FixedClock(), SequenceIds()
    run = await RunService(store, clock, ids).submit(
        "alice",
        "Task",
        RunOptions(
            routing_mode="two_stage",
        ),
    )
    model = ScriptedModel((response,))
    result = await AgentRunner(store, ModelGateway(model), clock, ids).execute("alice", run.run_id)
    assert result.status == RunStatus.FAILED
    assert result.error is not None and '"code":"validation"' in result.error.text
    assert len(model.requests) == 1
    async with store.transaction("alice") as tx:
        assert (await tx.steps(run.run_id))[0].response == response


@pytest.mark.asyncio
async def test_malformed_adapter_response(store: SQLiteStore) -> None:
    class Malformed(ScriptedModel):
        async def generate(self, request):
            return {"unexpected": "not a normalized response"}

    clock, ids = FixedClock(), SequenceIds()
    run = await RunService(store, clock, ids).submit(
        "alice",
        "Task",
        RunOptions(
            routing_mode="two_stage",
        ),
    )
    result = await AgentRunner(store, ModelGateway(Malformed(())), clock, ids).execute(
        "alice", run.run_id
    )
    assert result.status == RunStatus.FAILED
    async with store.transaction("alice") as tx:
        assert (await tx.steps(run.run_id))[0].status == StepStatus.FAILED


@pytest.mark.asyncio
async def test_steps_do_not_reset_on_continuation(store: SQLiteStore) -> None:
    clock, ids = FixedClock(), SequenceIds()
    run = await RunService(store, clock, ids).submit(
        "alice", "Task", RunOptions(routing_mode="two_stage", max_steps=1)
    )
    model = ScriptedModel((answer("Draft"), answer()))
    runner = AgentRunner(store, ModelGateway(model), clock, ids)
    result = await runner.execute("alice", run.run_id)
    assert result.status == RunStatus.FAILED
    assert result.error is not None and '"code":"budget_exceeded"' in result.error.text
    assert await runner.execute("alice", run.run_id) == result
    assert len(model.requests) == 1


class MovingClock(FixedClock):
    elapsed = 0

    def utc_now(self):
        return super().utc_now() + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return super().monotonic() + self.elapsed


@pytest.mark.asyncio
@pytest.mark.parametrize("expire_during_call", [False, True])
async def test_deadline_with_fake_clock(store: SQLiteStore, expire_during_call: bool) -> None:
    clock, ids = MovingClock(), SequenceIds()
    run = await RunService(store, clock, ids).submit(
        "alice", "Task", RunOptions(routing_mode="two_stage", timeout_seconds=5)
    )

    class SlowFake(ScriptedModel):
        async def generate(self, request):
            response = await super().generate(request)
            clock.elapsed = 6
            return response

    model = SlowFake((answer(),))
    if not expire_during_call:
        clock.elapsed = 6
    result = await AgentRunner(store, ModelGateway(model), clock, ids).execute("alice", run.run_id)
    assert result.status == RunStatus.FAILED
    assert result.error is not None and '"code":"deadline_exceeded"' in result.error.text
    assert len(model.requests) == int(expire_during_call)


class SimulatedCrash(RuntimeError):
    pass


class CrashAfterAcceptance:
    def __init__(self, store: SQLiteStore, run_id: str, stage: Stage) -> None:
        self.store, self.run_id, self.stage = store, run_id, stage

    @asynccontextmanager
    async def transaction(self, user_id: str) -> AsyncIterator[StateTransaction]:
        async with self.store.transaction(user_id) as tx:
            yield tx
            steps = await tx.steps(self.run_id)
            crash = bool(
                steps and steps[-1].stage == self.stage and steps[-1].status == StepStatus.ACCEPTED
            )
        if crash:
            raise SimulatedCrash("after durable response acceptance, before application")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [Stage.DECISION, Stage.RESPONSE])
async def test_restart_never_regenerates_accepted_response(tmp_path: Path, stage: Stage) -> None:
    path = tmp_path / "restart.sqlite"
    clock, ids = FixedClock(), SequenceIds()
    first = ScriptedModel((answer("Saved draft"), answer("Saved final")))
    async with SQLiteStore(path, clock, ids) as store:
        run = await RunService(store, clock, ids).submit(
            "alice",
            "Task",
            RunOptions(
                routing_mode="two_stage",
            ),
        )
        runner = AgentRunner(
            CrashAfterAcceptance(store, run.run_id, stage), ModelGateway(first), clock, ids
        )
        with pytest.raises(SimulatedCrash):
            await runner.execute("alice", run.run_id)
    resumed = ScriptedModel((answer("Saved final"),) if stage == Stage.DECISION else ())
    async with SQLiteStore(path, clock, ids) as store:
        result = await AgentRunner(store, ModelGateway(resumed), clock, ids).execute(
            "alice", run.run_id
        )
        assert result.status == RunStatus.COMPLETED
        assert result.result is not None and "Saved final" in result.result.text
        assert len(first.requests) + len(resumed.requests) == 2
        if resumed.requests:
            assert resumed.requests[0].model == "gemma3:1b"
            assert resumed.requests[0].messages[2].content == "Saved draft"


@pytest.mark.asyncio
async def test_cancelled_model_call_is_durable(store: SQLiteStore) -> None:
    entered, never = asyncio.Event(), asyncio.Event()

    class WaitingFake(ScriptedModel):
        async def generate(self, request):
            entered.set()
            await never.wait()
            return answer()

    clock, ids = FixedClock(), SequenceIds()
    run = await RunService(store, clock, ids).submit(
        "alice",
        "Task",
        RunOptions(
            routing_mode="two_stage",
        ),
    )
    runner = AgentRunner(store, ModelGateway(WaitingFake(())), clock, ids)
    async with asyncio.timeout(5):
        work = asyncio.create_task(runner.execute("alice", run.run_id))
        await entered.wait()
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
    async with store.transaction("alice") as tx:
        assert (await tx.get_run(run.run_id)).status == RunStatus.CANCELLED
        assert (await tx.steps(run.run_id))[0].status == StepStatus.FAILED


def test_context_keeps_complete_tool_groups() -> None:
    calls = Message(Role.ASSISTANT, "", (ToolCall("a", "calc", JsonObject()),))
    result = Message(Role.TOOL, "3", tool_call_id="a")
    history = (Message(Role.ASSISTANT, "old" * 100), calls, result)
    expected = build_context("system", "task", (calls, result), 1000)
    actual = build_context("system", "task", history, size(expected))
    assert actual == expected
    with pytest.raises(HarnessError):
        build_context("system", "task", (calls,), 1000)
    with pytest.raises(HarnessError) as error:
        build_context("system", "too long" * 100, (), 100)
    assert error.value.code == ErrorCode.BUDGET_EXCEEDED


def test_cli_offline_submit_inspect_events(tmp_path: Path) -> None:
    cli = CliRunner()
    database = str(tmp_path / "cli.sqlite")
    submitted = cli.invoke(
        app, ["run", "--task", "demo", "--user", "alice", "--database", database]
    )
    assert submitted.exit_code == 0, submitted.output
    run = json.loads(submitted.stdout)
    assert run["status"] == "completed"
    inspected = cli.invoke(
        app, ["inspect", run["run_id"], "--user", "alice", "--database", database]
    )
    assert inspected.exit_code == 0, inspected.output
    assert [s["model"] for s in json.loads(inspected.stdout)["steps"]] == [
        "gemma3:1b",
    ]
    events = cli.invoke(app, ["events", run["run_id"], "--user", "alice", "--database", database])
    assert events.exit_code == 0
    assert "step.accepted" in events.stdout
    assert (
        cli.invoke(
            app, ["inspect", run["run_id"], "--user", "bob", "--database", database]
        ).exit_code
        != 0
    )
