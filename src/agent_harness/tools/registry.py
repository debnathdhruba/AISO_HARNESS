import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.tooling import Invocation, ToolSpec

Handler = Callable[[Invocation], Awaitable[JsonObject]]
BlockingHandler = Callable[[Invocation], JsonObject]


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    execute: Handler | None = None
    blocking_execute: BlockingHandler | None = None

    def __post_init__(self) -> None:
        require(
            (self.execute is None) != (self.blocking_execute is None),
            "Register exactly one async or blocking tool handler",
        )


class ToolRegistry:
    def __init__(self, tools: tuple[RegisteredTool, ...]) -> None:
        keys = {(tool.spec.name, tool.spec.version) for tool in tools}
        require(len(keys) == len(tools), "Duplicate registered tool version")
        self._tools = {(tool.spec.name, tool.spec.version): tool for tool in tools}
        self._latest: dict[str, RegisteredTool] = {}
        for tool in tools:
            self._latest[tool.spec.name] = tool
            for schema in (tool.spec.input_schema, tool.spec.output_schema):
                parsed = json.loads(schema.text)
                # Schemas are trusted, local and cannot fetch remote references.
                require('"$ref"' not in schema.text, "External schema references are not supported")
                Draft202012Validator.check_schema(parsed)

    def get(self, name: str, version: str | None = None) -> RegisteredTool:
        tool = self._tools.get((name, version)) if version is not None else self._latest.get(name)
        if tool is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Tool/version is not registered")
        return tool

    def allowed(self, names: tuple[str, ...]) -> tuple[ToolSpec, ...]:
        return tuple(self.get(name).spec for name in names)

    @staticmethod
    def validate(value: JsonObject, schema: JsonObject) -> None:
        try:
            Draft202012Validator(json.loads(schema.text)).validate(json.loads(value.text))
        except ValidationError:
            raise HarnessError(
                ErrorCode.VALIDATION, "Tool data does not match its JSON Schema"
            ) from None
