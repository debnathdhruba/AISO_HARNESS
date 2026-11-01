import asyncio
import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from conftest import FixedClock, SequenceIds

from agent_harness.adapters.fake import ScriptedModel
from agent_harness.agents.runner import AgentRunner
from agent_harness.application.runs import RunService
from agent_harness.domain.entities import JsonObject, Run, Task
from agent_harness.domain.errors import HarnessError
from agent_harness.domain.model import (
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunOptions,
    ToolCall,
)
from agent_harness.domain.states import InvocationStatus, RunStatus
from agent_harness.domain.tooling import Effect, Invocation, ToolSpec, argument_hash
from agent_harness.memory.backends.sqlite import SQLiteStore
from agent_harness.models.gateway import ModelGateway
from agent_harness.tools.builtins import builtin_registry, calculate
from agent_harness.tools.executor import ToolExecutor, decide_approval
from agent_harness.tools.filesystem import FileSystemService
from agent_harness.tools.registry import RegisteredTool, ToolRegistry


def action(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        Message(Role.ASSISTANT, "Action prose is not final", calls), FinishReason.STOP
    )


def final(text: str = "Done") -> ModelResponse:
    return ModelResponse(Message(Role.ASSISTANT, text), FinishReason.STOP)


@pytest.mark.asyncio
async def test_invalid_disallowed_and_repeated_calls_become_observations(
    store: SQLiteStore,
) -> None:
    clock, ids = FixedClock(), SequenceIds()
    registry = builtin_registry()
    calls = (
        ToolCall("bad", "calculator", JsonObject('{"wrong":1}')),
        ToolCall("denied", "shell", JsonObject()),
        ToolCall("first", "calculator", JsonObject('{"expression":"1+2"}')),
        ToolCall("repeat", "calculator", JsonObject('{"expression":"1+2"}')),
    )
    model = ScriptedModel((action(*calls), final("draft"), final()))
    run = await RunService(store, clock, ids).submit(
        "alice", "Task", RunOptions(routing_mode="tools", repeated_action_limit=1)
    )
    result = await AgentRunner(
        store, ModelGateway(model), clock, ids, ToolExecutor(store, registry, clock)
    ).execute("alice", run.run_id)
    assert result.status == RunStatus.COMPLETED
    async with store.transaction("alice") as tx:
        invocations = await tx.invocations(run.run_id)
    assert [i.status for i in invocations] == [
        InvocationStatus.FAILED,
        InvocationStatus.FAILED,
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
    ]
    assert len([m for m in model.requests[1].messages if m.role == Role.TOOL]) == 4
    assert all(not request.tools for request in model.requests if request.model == "gemma3:1b")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exception", "oversized", "timeout"])
async def test_tool_errors_are_bounded(store: SQLiteStore, mode: str) -> None:
    async def handler(invocation: Invocation) -> JsonObject:
        if mode == "exception":
            raise ValueError("private diagnostic")
        if mode == "timeout":
            await asyncio.Event().wait()
        return JsonObject(json.dumps({"text": "x" * 1000}))

    spec = ToolSpec(
        "fault",
        "1",
        "test",
        JsonObject('{"type":"object"}'),
        JsonObject('{"type":"object"}'),
        timeout_seconds=0.01,
        max_output_bytes=128,
    )
    registry = ToolRegistry((RegisteredTool(spec, handler),))
    clock, ids = FixedClock(), SequenceIds()
    run = await RunService(store, clock, ids).submit(
        "alice", "Task", RunOptions(routing_mode="tools", tool_allowlist=("fault",))
    )
    model = ScriptedModel((action(ToolCall("fault", "fault", JsonObject())), final(), final()))
    result = await AgentRunner(
        store, ModelGateway(model), clock, ids, ToolExecutor(store, registry, clock)
    ).execute("alice", run.run_id)
    assert result.status == RunStatus.COMPLETED
    async with store.transaction("alice") as tx:
        invocation = (await tx.invocations(run.run_id))[0]
    assert invocation.status == InvocationStatus.FAILED
    assert "private diagnostic" not in invocation.error.text


@pytest.mark.asyncio
async def test_interrupted_write_is_unknown_and_never_replayed(store: SQLiteStore) -> None:
    class PowerLoss(BaseException):
        pass

    executions = 0

    async def handler(invocation: Invocation) -> JsonObject:
        nonlocal executions
        executions += 1
        raise PowerLoss()

    spec = ToolSpec("unsafe", "1", "test", JsonObject(), JsonObject(), Effect.NON_IDEMPOTENT_WRITE)
    clock, ids = FixedClock(), SequenceIds()
    executor = ToolExecutor(store, ToolRegistry((RegisteredTool(spec, handler),)), clock)
    run = await RunService(store, clock, ids).submit(
        "alice", "Task", RunOptions(routing_mode="tools", tool_allowlist=("unsafe",))
    )
    model = ScriptedModel((action(ToolCall("write", "unsafe", JsonObject())),))
    with pytest.raises(PowerLoss):
        await AgentRunner(store, ModelGateway(model), clock, ids, executor).execute(
            "alice", run.run_id
        )
    resumed = AgentRunner(store, ModelGateway(ScriptedModel(())), clock, ids, executor)
    result = await resumed.execute("alice", run.run_id)
    assert result.status == RunStatus.FAILED and executions == 1
    async with store.transaction("alice") as tx:
        assert (await tx.invocations(run.run_id))[0].status == InvocationStatus.UNKNOWN


def invocation_for(run: Run, task: Task, number: int = 9) -> Invocation:
    arguments = JsonObject()
    return Invocation(
        str(UUID(int=number)),
        run.run_id,
        task.task_id,
        run.user_id,
        str(UUID(int=8)),
        "call",
        "read_file",
        "1",
        arguments,
        argument_hash(arguments),
        Effect.IDEMPOTENT_WRITE,
    )


def test_builtin_registry_registers_filesystem_tools() -> None:
    registry = builtin_registry()
    assert {
        "read_file",
        "write_file",
        "search_files",
        "edit_file",
    } <= {
        spec.name
        for spec in registry.allowed(("read_file", "write_file", "search_files", "edit_file"))
    }
    assert registry.get("write_file").spec.effect == Effect.NON_IDEMPOTENT_WRITE
    assert registry.get("edit_file").spec.effect == Effect.NON_IDEMPOTENT_WRITE


@pytest.mark.asyncio
async def test_filesystem_tools_read_write_edit_and_search(
    tmp_path: Path, run: Run, task: Task
) -> None:
    root = tmp_path / "outside-workspace"
    root.mkdir()
    files = FileSystemService()
    invocation = invocation_for(run, task)
    path = root / "src" / "example.py"
    written = json.loads((await files.write(invocation, str(path), "value = 1\nvalue = 2\n")).text)
    assert written == {"path": str(path), "bytes_written": len(b"value = 1\nvalue = 2\n")}
    assert json.loads((await files.read(invocation, str(path))).text)["content"] == (
        "value = 1\nvalue = 2\n"
    )
    with pytest.raises(HarnessError, match="exactly once"):
        await files.edit(invocation, str(path), "value", "result")
    edited = json.loads((await files.edit(invocation, str(path), "value = 1", "result = 1")).text)
    assert edited["replacements"] == 1
    results = json.loads((await files.search("result = 1", "*.py", str(root))).text)
    assert results["matches"] == [{"path": str(path), "line": 1, "text": "result = 1"}]
    assert results["skipped_non_utf8"] == 0


@pytest.mark.asyncio
async def test_filesystem_tools_can_access_paths_outside_project_and_through_symlinks(
    tmp_path: Path, run: Run, task: Task
) -> None:
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("private")
    linked = root / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(outside)], capture_output=True
        )
        assert result.returncode == 0, result.stderr

    files = FileSystemService()
    invocation = invocation_for(run, task)
    through_link = linked / "secret.txt"
    assert (
        json.loads((await files.read(invocation, str(outside / "secret.txt"))).text)["content"]
        == "private"
    )
    assert (
        json.loads((await files.read(invocation, str(through_link))).text)["content"] == "private"
    )
    await files.write(invocation, str(outside / "secret.txt"), "changed")
    assert (outside / "secret.txt").read_text() == "changed"


@pytest.mark.asyncio
async def test_filesystem_tools_can_read_any_local_path(
    tmp_path: Path, run: Run, task: Task
) -> None:
    root = tmp_path / "workspace"
    (root / ".local").mkdir(parents=True)
    (root / ".local" / "state.sqlite").write_text("runtime-secret")
    files = FileSystemService()
    invocation = invocation_for(run, task)

    path = root / ".local" / "state.sqlite"
    assert json.loads((await files.read(invocation, str(path))).text)["content"] == "runtime-secret"


@pytest.mark.asyncio
async def test_filesystem_search_reports_file_budget_truncation(
    tmp_path: Path, run: Run, task: Task
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("needle")
    (root / "b.txt").write_text("needle")
    files = FileSystemService(max_search_files=1, max_search_bytes=1024)
    result = json.loads((await files.search("needle", "*.txt", str(root))).text)
    assert result["scanned_files"] == 1
    assert result["matches"] == [{"path": str(root / "a.txt"), "line": 1, "text": "needle"}]
    assert result["truncated"] is True


@pytest.mark.parametrize("expression", ["__import__('os')", "2**1000", "1/0", "True", "9" * 201])
def test_calculator_rejects_unsafe_or_unbounded_expressions(expression: str) -> None:
    with pytest.raises(HarnessError):
        calculate(expression)


@pytest.mark.asyncio
async def test_final_prose_cannot_satisfy_required_tool(store: SQLiteStore) -> None:
    clock, ids = FixedClock(), SequenceIds()
    registry = builtin_registry()
    model = ScriptedModel((final("42"), final("42")))
    run = await RunService(store, clock, ids).submit(
        "alice",
        "Use calculator",
        RunOptions(routing_mode="tools", required_tools=("calculator",), max_steps=2),
    )
    result = await AgentRunner(
        store, ModelGateway(model), clock, ids, ToolExecutor(store, registry, clock)
    ).execute("alice", run.run_id)
    assert result.status == RunStatus.FAILED
    assert all(r.model == "lfm2.5-thinking:1.2b" for r in model.requests)
    assert all([t.name for t in r.tools] == ["calculator"] for r in model.requests)
