"""Contracts and dependency wiring for the built-in tool catalog."""

import json

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.memory import MemorySharing
from agent_harness.domain.tooling import Effect, ToolSpec
from agent_harness.tools.filesystem import FileSystemService
from agent_harness.tools.registry import Handler, RegisteredTool, ToolRegistry
from agent_harness.tools.tools import (
    CalculatorTool,
    CallbackTool,
    EditFileTool,
    ListFilesTool,
    ReadFileRangeTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
    calculate,
)


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


def delegation_schema(agent_profiles: tuple[str, ...]) -> JsonObject:
    return schema(
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
                        "agent_definition_id": {"type": "string", "enum": list(agent_profiles)},
                        "agent_version": {"type": "string", "maxLength": 20},
                        "objective": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "acceptance_criteria": {"type": "object"},
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
    )


def builtin_registry(
    delegate: Handler | None = None,
    memory_save: Handler | None = None,
) -> ToolRegistry:
    """Build the application catalog by injecting services into tool handlers."""
    text = {"type": "string", "maxLength": 4096}
    output = JsonObject('{"type":"object"}')
    files = FileSystemService()

    tools = [
        RegisteredTool(
            ToolSpec(
                "calculator",
                "1",
                "Compute basic arithmetic (+ - * / %).",
                schema({"expression": {"type": "string", "maxLength": 200}}, ["expression"]),
                output,
            ),
            CalculatorTool(),
        ),
        RegisteredTool(
            ToolSpec(
                "read_file_range",
                "1",
                "Read a bounded line range from a UTF-8 local file. Use this instead of "
                "reading an entire large file when only part is relevant.",
                schema(
                    {
                        "path": {"type": "string", "maxLength": 512},
                        "start_line": {"type": "integer", "minimum": 1},
                        "line_count": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    ["path", "start_line", "line_count"],
                ),
                output,
                max_output_bytes=32_768,
            ),
            ReadFileRangeTool(files),
        ),
        RegisteredTool(
            ToolSpec(
                "list_files",
                "1",
                "List bounded files and directories directly inside any local directory. "
                "Use an absolute path or a path relative to the current directory; this does "
                "not recurse.",
                schema(
                    {
                        "directory": {"type": "string", "maxLength": 512},
                        "pattern": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                            "default": 100,
                        },
                    },
                    ["directory"],
                ),
                output,
                max_output_bytes=32_768,
            ),
            ListFilesTool(files),
        ),
        RegisteredTool(
            ToolSpec(
                "read_file",
                "1",
                "Read a bounded UTF-8 text file at an absolute path or relative to the current "
                "directory.",
                schema({"path": {"type": "string", "maxLength": 512}}, ["path"]),
                output,
                max_output_bytes=65_536,
            ),
            ReadFileTool(files),
        ),
        RegisteredTool(
            ToolSpec(
                "write_file",
                "1",
                "Write bounded UTF-8 text to any local file path.",
                schema(
                    {"path": {"type": "string", "maxLength": 512}, "content": text},
                    ["path", "content"],
                ),
                output,
                effect=Effect.NON_IDEMPOTENT_WRITE,
            ),
            WriteFileTool(files),
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
                        "path": {"type": "string", "maxLength": 1024, "default": "."},
                    },
                    ["query", "pattern"],
                ),
                output,
                max_output_bytes=32_768,
            ),
            SearchFilesTool(files),
        ),
        RegisteredTool(
            ToolSpec(
                "edit_file",
                "1",
                "Replace one exact, unique text match in a local file.",
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
            EditFileTool(files),
        ),
    ]

    if delegate is not None:
        handler = CallbackTool(delegate)
        tools.extend(
            (
                RegisteredTool(
                    ToolSpec(
                        "delegate_tasks",
                        "1",
                        "Delegate a bounded batch of explicit tasks to registered child agents. "
                        "Dependencies use aliases from this batch.",
                        delegation_schema(("coordinator", "researcher", "writer")),
                        output,
                        effect=Effect.IDEMPOTENT_WRITE,
                        idempotency="invocation_id",
                        supports_reconciliation=True,
                    ),
                    handler,
                ),
                RegisteredTool(
                    ToolSpec(
                        "delegate_tasks",
                        "2",
                        "Delegate a bounded batch of tasks to task-focused worker agents. Use for "
                        "substantial, multi-part, or context-heavy work; put independent tasks in "
                        "one batch for parallel execution. Dependencies use aliases from this "
                        "batch.",
                        delegation_schema(("worker",)),
                        output,
                        effect=Effect.IDEMPOTENT_WRITE,
                        idempotency="invocation_id",
                        supports_reconciliation=True,
                    ),
                    handler,
                ),
            )
        )

    if memory_save is not None:
        tools.append(
            RegisteredTool(
                ToolSpec(
                    "memory_save",
                    "1",
                    "Save a concise fact only when the user explicitly asks you to remember it. "
                    "Use shared only when the user explicitly asks to share it with their agents.",
                    schema(
                        {
                            "content": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "sharing": {
                                "type": "string",
                                "enum": [MemorySharing.PRIVATE.value, MemorySharing.SHARED.value],
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1, "maxLength": 80},
                                "maxItems": 20,
                                "uniqueItems": True,
                            },
                        },
                        ["content", "sharing"],
                    ),
                    output,
                    effect=Effect.IDEMPOTENT_WRITE,
                    idempotency="invocation_id",
                    supports_reconciliation=True,
                ),
                CallbackTool(memory_save),
            )
        )

    return ToolRegistry(tuple(tools))


__all__ = ("builtin_registry", "calculate", "schema")
