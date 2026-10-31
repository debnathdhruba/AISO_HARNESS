"""Trusted TOML/environment/CLI configuration, isolated from domain contracts."""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import RunOptions


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    database: Path = Path(".local/harness.sqlite")
    ollama_url: str = "http://127.0.0.1:11434"
    provider: Literal["fake", "ollama"] = "fake"
    decision_model: str = Field(default="lfm2.5-thinking:1.2b", min_length=1)
    response_model: str = Field(default="gemma3:1b", min_length=1)
    max_steps: int = Field(default=8, ge=1, le=100)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    max_output_tokens: int = Field(default=1024, ge=1, le=8192)
    max_input_bytes: int = Field(default=16384, ge=256, le=131072)
    tool_allowlist: tuple[str, ...] = (
        "calculator",
        "artifact_write",
        "artifact_read",
        "artifact_list",
        "read_file",
        "write_file",
        "search_files",
        "edit_file",
    )
    max_tool_calls: int = Field(default=8, ge=1, le=100)
    repeated_action_limit: int = Field(default=2, ge=1, le=10)
    require_write_approval: bool = False
    workspace: str = "."
    artifact_root: str = ".local/artifacts"
    required_tools: tuple[str, ...] = ()
    routing_mode: Literal["auto", "chat", "tools", "two_stage"] = "auto"
    response_max_tokens: int = Field(default=512, ge=1, le=8192)
    model_concurrency: int = Field(default=2, ge=1, le=32)
    model_queue_capacity: int = Field(default=8, ge=0, le=1024)
    model_provider_concurrency: int = Field(default=1, ge=1, le=32)
    tool_concurrency: int = Field(default=4, ge=1, le=64)
    tool_queue_capacity: int = Field(default=16, ge=0, le=1024)
    artifact_concurrency: int = Field(default=2, ge=1, le=32)
    artifact_queue_capacity: int = Field(default=8, ge=0, le=1024)
    memory_concurrency: int = Field(default=2, ge=1, le=32)
    memory_queue_capacity: int = Field(default=8, ge=0, le=1024)
    blocking_tool_workers: int = Field(default=2, ge=1, le=32)
    blocking_tool_queue_capacity: int = Field(default=8, ge=0, le=1024)

    def run_options(self) -> RunOptions:
        return RunOptions(
            **self.model_dump(
                exclude={
                    "database",
                    "ollama_url",
                    "model_concurrency",
                    "model_queue_capacity",
                    "model_provider_concurrency",
                    "tool_concurrency",
                    "tool_queue_capacity",
                    "artifact_concurrency",
                    "artifact_queue_capacity",
                    "memory_concurrency",
                    "memory_queue_capacity",
                    "blocking_tool_workers",
                    "blocking_tool_queue_capacity",
                }
            )
        )


def load_settings(path: Path | None = None, **overrides: object) -> Settings:
    try:
        values: dict[str, object] = {}
        if path is not None:
            with path.open("rb") as stream:
                values.update(tomllib.load(stream))
        for name in Settings.model_fields:
            value = os.environ.get(f"AGENT_HARNESS_{name.upper()}")
            if value is not None:
                values[name] = value
        values.update({key: value for key, value in overrides.items() if value is not None})
        return Settings.model_validate(values)
    except (OSError, ValueError, ValidationError):
        raise HarnessError(
            ErrorCode.VALIDATION, "Invalid configuration or unreadable TOML file"
        ) from None
