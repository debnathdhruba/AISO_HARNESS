import ast
import json
import math
import operator
from collections.abc import Callable
from pathlib import Path
from collections.abc import Awaitable

from agent_harness.artifacts.service import ArtifactService
from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import require
from agent_harness.domain.tooling import Effect, Invocation, ToolSpec
from agent_harness.tools.registry import RegisteredTool, ToolRegistry
from agent_harness.tools.workspace import WorkspaceFileService


def schema(properties: dict[str, object], required: list[str]) -> JsonObject:
    return JsonObject(
        json.dumps(
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
        )
    )


def calculate(expression: str) -> int | float:
    require(len(expression) <= 200, "Calculator expression too long")
    try:
        node = ast.parse(expression, mode="eval")
    except SyntaxError:
        require(False, "Invalid arithmetic expression")
        raise AssertionError("unreachable") from None
    require(len(list(ast.walk(node))) <= 64, "Calculator expression too complex")
    operations: dict[type[ast.operator], Callable[[float, float], float]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
    }

    def evaluate(value: ast.AST) -> float:
        if isinstance(value, ast.Constant) and type(value.value) in {int, float}:
            assert isinstance(value.value, (int, float))
            result = float(value.value)
        elif isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.UAdd, ast.USub)):
            result = evaluate(value.operand) * (-1 if isinstance(value.op, ast.USub) else 1)
        elif isinstance(value, ast.BinOp) and type(value.op) in operations:
            result = operations[type(value.op)](evaluate(value.left), evaluate(value.right))
        else:
            require(False, "Only bounded basic arithmetic is supported")
            raise AssertionError("unreachable")
        require(math.isfinite(result) and abs(result) <= 1e12, "Calculator magnitude exceeds limit")
        return result

    try:
        result = evaluate(node.body)
    except (ZeroDivisionError, OverflowError):
        require(False, "Invalid arithmetic operation")
        raise AssertionError("unreachable") from None
    return int(result) if result.is_integer() else result


def builtin_registry(
    artifacts: ArtifactService,
    workspace: Path,
    artifact_root: Path,
    delegate: Callable[[Invocation], Awaitable[JsonObject]] | None = None,
) -> ToolRegistry:
    text = {"type": "string", "maxLength": 4096}
    output = JsonObject('{"type":"object"}')
    files = WorkspaceFileService(workspace, artifact_root)

    async def calculator(invocation: Invocation) -> JsonObject:
        args = json.loads(invocation.arguments.text)
        return JsonObject(json.dumps({"value": calculate(args["expression"])}))

    async def write(invocation: Invocation) -> JsonObject:
        args = json.loads(invocation.arguments.text)
        return await artifacts.write(invocation, args["name"], args["content"])

    async def read(invocation: Invocation) -> JsonObject:
        return await artifacts.read(
            invocation, json.loads(invocation.arguments.text)["artifact_id"]
        )

    async def listing(invocation: Invocation) -> JsonObject:
        return await artifacts.list(invocation)

    async def read_file(invocation: Invocation) -> JsonObject:
        args = json.loads(invocation.arguments.text)
        return await files.read(invocation, args["path"])

    async def write_file(invocation: Invocation) -> JsonObject:
        args = json.loads(invocation.arguments.text)
        return await files.write(invocation, args["path"], args["content"])

    async def search_files(invocation: Invocation) -> JsonObject:
        args = json.loads(invocation.arguments.text)
        return await files.search(args["query"], args["pattern"])

    async def edit_file(invocation: Invocation) -> JsonObject:
        args = json.loads(invocation.arguments.text)
        return await files.edit(invocation, args["path"], args["old_text"], args["new_text"])

    tools = [
            RegisteredTool(
                ToolSpec(
                    "calculator",
                    "1",
                    "Compute basic arithmetic (+ - * / %).",
                    schema({"expression": {"type": "string", "maxLength": 200}}, ["expression"]),
                    output,
                ),
                calculator,
            ),
            RegisteredTool(
                ToolSpec(
                    "artifact_write",
                    "1",
                    "Write a bounded text artifact in this task.",
                    schema(
                        {"name": {"type": "string", "maxLength": 100}, "content": text},
                        ["name", "content"],
                    ),
                    output,
                    effect=Effect.IDEMPOTENT_WRITE,
                    idempotency="invocation_id",
                    supports_reconciliation=True,
                ),
                write,
            ),
            RegisteredTool(
                ToolSpec(
                    "artifact_read",
                    "1",
                    "Read an artifact by its returned ID.",
                    schema({"artifact_id": {"type": "string"}}, ["artifact_id"]),
                    output,
                ),
                read,
            ),
            RegisteredTool(
                ToolSpec(
                    "artifact_list", "1", "List artifacts in this task.", schema({}, []), output
                ),
                listing,
            ),
            RegisteredTool(
                ToolSpec(
                    "read_file",
                    "1",
                    "Read a bounded UTF-8 text file inside the configured workspace.",
                    schema({"path": {"type": "string", "maxLength": 512}}, ["path"]),
                    output,
                    max_output_bytes=65_536,
                ),
                read_file,
            ),
            RegisteredTool(
                ToolSpec(
                    "write_file",
                    "1",
                    "Write bounded UTF-8 text to a file inside the configured workspace.",
                    schema(
                        {
                            "path": {"type": "string", "maxLength": 512},
                            "content": text,
                        },
                        ["path", "content"],
                    ),
                    output,
                    effect=Effect.NON_IDEMPOTENT_WRITE,
                ),
                write_file,
            ),
            RegisteredTool(
                ToolSpec(
                    "search_files",
                    "1",
                    "Search UTF-8 files with a literal query and glob pattern.",
                    schema(
                        {
                            "query": {"type": "string", "minLength": 1, "maxLength": 512},
                            "pattern": {"type": "string", "maxLength": 200},
                        },
                        ["query", "pattern"],
                    ),
                    output,
                    max_output_bytes=32_768,
                ),
                search_files,
            ),
            RegisteredTool(
                ToolSpec(
                    "edit_file",
                    "1",
                    "Replace one exact, unique text match in a workspace file.",
                    schema(
                        {
                            "path": {"type": "string", "maxLength": 512},
                            "old_text": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "new_text": {"type": "string", "maxLength": 4096},
                        },
                        ["path", "old_text", "new_text"],
                    ),
                    output,
                    effect=Effect.NON_IDEMPOTENT_WRITE,
                ),
                edit_file,
            ),
    ]
    if delegate is not None:
        tools.append(
            RegisteredTool(
                ToolSpec(
                    "delegate_tasks",
                    "1",
                    "Delegate a bounded batch of explicit tasks to registered child agents. "
                    "Dependencies use aliases from this batch.",
                    schema(
                        {
                            "tasks": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 8,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "alias": {"type": "string", "minLength": 1, "maxLength": 64},
                                        "idempotency_key": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 200,
                                        },
                                        "agent_definition_id": {
                                            "type": "string",
                                            "enum": ["coordinator", "researcher", "writer"],
                                        },
                                        "agent_version": {"type": "string", "maxLength": 20},
                                        "objective": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 4096,
                                        },
                                        "acceptance_criteria": {"type": "object"},
                                        "input_artifact_ids": {
                                            "type": "array",
                                            "items": {"type": "string", "maxLength": 64},
                                            "maxItems": 16,
                                        },
                                        "dependencies": {
                                            "type": "array",
                                            "items": {"type": "string", "maxLength": 64},
                                            "maxItems": 8,
                                        },
                                        "capabilities": {
                                            "type": "array",
                                            "items": {"type": "string", "maxLength": 64},
                                            "maxItems": 16,
                                        },
                                        "max_steps": {"type": "integer", "minimum": 1, "maximum": 100},
                                    },
                                    "required": [
                                        "alias",
                                        "idempotency_key",
                                        "agent_definition_id",
                                        "objective",
                                    ],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        ["tasks"],
                    ),
                    output,
                    effect=Effect.IDEMPOTENT_WRITE,
                    idempotency="invocation_id",
                    supports_reconciliation=True,
                ),
                delegate,
            )
        )
    return ToolRegistry(tuple(tools))
