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
        require(len({t.spec.name for t in tools}) == len(tools), "Duplicate registered tool name")
        self._tools = {tool.spec.name: tool for tool in tools}
        for tool in tools:
            for schema in (tool.spec.input_schema, tool.spec.output_schema):
                parsed = json.loads(schema.text)
                # Schemas are trusted, local and cannot fetch remote references.
                require('"$ref"' not in schema.text, "External schema references are not supported")
                Draft202012Validator.check_schema(parsed)

    def get(self, name: str, version: str | None = None) -> RegisteredTool:
        tool = self._tools.get(name)
        if tool is None or (version is not None and tool.spec.version != version):
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
