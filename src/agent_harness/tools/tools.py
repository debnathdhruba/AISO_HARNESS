"""Implementations for the built-in agent tools."""

import ast
import json
import math
import operator
from collections.abc import Awaitable, Callable
from typing import cast

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import require
from agent_harness.domain.tooling import Invocation
from agent_harness.tools.filesystem import FileSystemService


def calculate(expression: str) -> int | float:
    """Evaluate bounded basic arithmetic without executing Python code."""
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


def _arguments(invocation: Invocation) -> dict[str, object]:
    value = json.loads(invocation.arguments.text)
    require(isinstance(value, dict), "Tool arguments must be a JSON object")
    return cast(dict[str, object], value)


class CalculatorTool:
    async def __call__(self, invocation: Invocation) -> JsonObject:
        args = _arguments(invocation)
        return JsonObject(json.dumps({"value": calculate(str(args["expression"]))}))


class ReadFileTool:
    def __init__(self, filesystem: FileSystemService) -> None:
        self._filesystem = filesystem

    async def __call__(self, invocation: Invocation) -> JsonObject:
        return await self._filesystem.read(invocation, str(_arguments(invocation)["path"]))


class ReadFileRangeTool:
    def __init__(self, filesystem: FileSystemService) -> None:
        self._filesystem = filesystem

    async def __call__(self, invocation: Invocation) -> JsonObject:
        args = _arguments(invocation)
        return self._filesystem.read_range(
            str(args["path"]), cast(int, args["start_line"]), cast(int, args["line_count"])
        )


class ListFilesTool:
    def __init__(self, filesystem: FileSystemService) -> None:
        self._filesystem = filesystem

    async def __call__(self, invocation: Invocation) -> JsonObject:
        args = _arguments(invocation)
        return self._filesystem.list_files(
            str(args["directory"]),
            str(args.get("pattern", "*")),
            cast(int, args.get("limit", 100)),
        )


class WriteFileTool:
    def __init__(self, filesystem: FileSystemService) -> None:
        self._filesystem = filesystem

    async def __call__(self, invocation: Invocation) -> JsonObject:
        args = _arguments(invocation)
        return await self._filesystem.write(invocation, str(args["path"]), str(args["content"]))


class SearchFilesTool:
    def __init__(self, filesystem: FileSystemService) -> None:
        self._filesystem = filesystem

    async def __call__(self, invocation: Invocation) -> JsonObject:
        args = _arguments(invocation)
        return await self._filesystem.search(
            str(args["query"]), str(args["pattern"]), str(args.get("path", "."))
        )


class EditFileTool:
    def __init__(self, filesystem: FileSystemService) -> None:
        self._filesystem = filesystem

    async def __call__(self, invocation: Invocation) -> JsonObject:
        args = _arguments(invocation)
        return await self._filesystem.edit(
            invocation,
            str(args["path"]),
            str(args["old_text"]),
            str(args["new_text"]),
        )


class CallbackTool:
    """Adapt an application-owned operation to the registered tool handler contract."""

    def __init__(self, callback: Callable[[Invocation], Awaitable[JsonObject]]) -> None:
        self._callback = callback

    async def __call__(self, invocation: Invocation) -> JsonObject:
        return await self._callback(invocation)
