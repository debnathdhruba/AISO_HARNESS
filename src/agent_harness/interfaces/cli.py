"""Trusted-local CLI. No command accepts model text as authority or configuration."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from agent_harness.bootstrap import application, runner
from agent_harness.config import Settings, load_settings
from agent_harness.domain.entities import Run
from agent_harness.domain.errors import HarnessError
from agent_harness.domain.model import RunOptions
from agent_harness.domain.states import RunStatus
from agent_harness.models.codec import encode_step
from agent_harness.tools.executor import decide_approval

app = typer.Typer(no_args_is_help=True, help="Standalone agent harness: offline or local Ollama.")


def summary(run: Run) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "user_id": run.user_id,
        "status": run.status.value,
        "version": run.version,
        "deadline": run.deadline.isoformat() if run.deadline else None,
        "models": json.loads(run.config.model.text),
        "result": json.loads(run.result.text) if run.result else None,
        "error": json.loads(run.error.text) if run.error else None,
    }


async def execute(settings: Settings, user: str, task: str | None, run_id: str | None) -> Run:
    async with application(settings) as services:
        if run_id is None:
            assert task is not None
            run = await services.runs.submit(user, task, settings.run_options())
        else:
            run = await services.runs.inspect(user, run_id)
        typer.echo(f"run_id={run.run_id}", err=True)
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.PAUSED,
        }:
            return run
        async with runner(services, settings, RunOptions.restore(run.config.model)) as agent:
            return await agent.execute(user, run.run_id)


@app.command("run")
def run_command(
    task: Annotated[str, typer.Option(help="Task input")],
    user: Annotated[str, typer.Option(help="Trusted-local principal")],
    provider: Annotated[str | None, typer.Option(help="fake (offline) or ollama")] = None,
    database: Annotated[Path | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option(help="TOML settings path")] = None,
    max_steps: Annotated[int | None, typer.Option()] = None,
    timeout: Annotated[int | None, typer.Option(help="Total run deadline in seconds")] = None,
    require_write_approval: Annotated[bool | None, typer.Option()] = None,
    workspace: Annotated[str | None, typer.Option()] = None,
    mode: Annotated[str | None, typer.Option(help="auto, chat, tools, or two_stage")] = None,
) -> None:
    settings = load_settings(
        config,
        provider=provider,
        database=database,
        max_steps=max_steps,
        timeout_seconds=timeout,
        require_write_approval=require_write_approval,
        workspace=workspace,
        routing_mode=mode,
    )
    result = asyncio.run(execute(settings, user, task, None))
    typer.echo(json.dumps(summary(result), indent=2))
    if result.status != RunStatus.COMPLETED:
        raise typer.Exit(1)


@app.command("continue")
def continue_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database: Annotated[Path | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Continue an interrupted Phase 2 run using its original deadline and models."""
    result = asyncio.run(execute(load_settings(config, database=database), user, None, run_id))
    typer.echo(json.dumps(summary(result), indent=2))
    if result.status != RunStatus.COMPLETED:
        raise typer.Exit(1)


async def inspect_run(
    settings: Settings, user: str, run_id: str, history: bool
) -> dict[str, object]:
    async with application(settings) as services:
        run = await services.runs.inspect(user, run_id)
        result = summary(run)
        async with services.store.transaction(user) as tx:
            steps = await tx.steps(run_id)
            invocations = await tx.invocations(run_id)
            artifacts = await tx.artifacts(run_id)
        result["invocations"] = [
            {
                "invocation_id": i.invocation_id,
                "tool": i.tool_name,
                "version": i.tool_version,
                "step_id": i.step_id,
                "call_id": i.call_id,
                "argument_hash": i.argument_hash,
                "arguments": json.loads(i.arguments.text),
                "status": i.status.value,
                "result": json.loads(i.result.text) if i.result else None,
                "error": json.loads(i.error.text) if i.error else None,
                "approval_expires_at": i.approval_expires_at.isoformat()
                if i.approval_expires_at
                else None,
            }
            for i in invocations
        ]
        result["artifacts"] = [
            {
                "artifact_id": a.artifact_id,
                "name": a.name,
                "version": a.version,
                "status": a.status,
                "checksum": a.checksum,
            }
            for a in artifacts
        ]
        result["steps"] = [
            json.loads(encode_step(step))["data"]
            if history
            else {
                "ordinal": step.ordinal,
                "model": step.request.model,
                "stage": step.stage.value,
                "status": step.status.value,
                "usage": {
                    "input_tokens": step.response.usage.input_tokens,
                    "output_tokens": step.response.usage.output_tokens,
                }
                if step.response
                else None,
            }
            for step in steps
        ]
        return result


@app.command("inspect")
def inspect_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database: Annotated[Path | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option()] = None,
    history: Annotated[
        bool, typer.Option(help="Include private persisted prompts and responses")
    ] = False,
) -> None:
    value = asyncio.run(
        inspect_run(load_settings(config, database=database), user, run_id, history)
    )
    typer.echo(json.dumps(value, indent=2))


@app.command("trace")
def trace_command(
    run_id: str,
    user: Annotated[str, typer.Option(help="Trusted-local principal")],
    database: Annotated[Path | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Show the persisted model, tool and lifecycle trace for one run."""

    async def read() -> dict[str, object]:
        settings = load_settings(config, database=database)
        value = await inspect_run(settings, user, run_id, history=True)
        async with application(settings) as services:
            async with services.store.transaction(user) as tx:
                events = []
                after = 0
                while True:
                    batch = await tx.events(run_id, after=after, limit=1000)
                    events.extend(batch)
                    if len(batch) < 1000:
                        break
                    after = batch[-1].sequence
        value["events"] = [
            {
                "sequence": event.sequence,
                "type": event.event_type,
                "task_id": event.task_id,
                "timestamp": event.timestamp.isoformat(),
                "payload": json.loads(event.payload.text),
            }
            for event in events
        ]
        return value

    typer.echo(json.dumps(asyncio.run(read()), indent=2))


@app.command("events")
def events_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database: Annotated[Path | None, typer.Option()] = None,
    after: Annotated[int, typer.Option()] = 0,
) -> None:
    async def read() -> list[dict[str, object]]:
        async with application(load_settings(database=database)) as services:
            await services.runs.inspect(user, run_id)
            async with services.store.transaction(user) as tx:
                events = await tx.events(run_id, after=after)
            return [
                {
                    "sequence": e.sequence,
                    "type": e.event_type,
                    "payload": json.loads(e.payload.text),
                }
                for e in events
            ]

    typer.echo(json.dumps(asyncio.run(read()), indent=2))


def approval_command(
    invocation_id: str, user: str, digest: str, database: Path | None, approve: bool
) -> None:
    async def decide() -> None:
        async with application(load_settings(database=database)) as services:
            await decide_approval(services.store, user, invocation_id, digest, approve)

    asyncio.run(decide())
    typer.echo("Approval recorded. Use continue RUN_ID to advance the run.")


@app.command("approve")
def approve_command(
    invocation_id: str,
    user: Annotated[str, typer.Option()],
    argument_hash: Annotated[str, typer.Option()],
    database: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Approve the exact invocation and argument hash shown by inspect."""
    approval_command(invocation_id, user, argument_hash, database, True)


@app.command("deny")
def deny_command(
    invocation_id: str,
    user: Annotated[str, typer.Option()],
    argument_hash: Annotated[str, typer.Option()],
    database: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Deny an invocation; continuation feeds the denial back to the agent."""
    approval_command(invocation_id, user, argument_hash, database, False)


def main() -> None:
    try:
        app()
    except HarnessError as error:
        typer.echo(
            json.dumps(
                {
                    "error": {
                        "code": error.code.value,
                        "message": error.message,
                        "retryable": error.retryable,
                    }
                }
            ),
            err=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
