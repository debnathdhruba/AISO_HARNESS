"""Trusted TOML/environment/CLI configuration, isolated from domain contracts."""

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import RunOptions


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mongodb_uri: SecretStr = SecretStr("mongodb://127.0.0.1:27017")
    database_name: str = Field(default="test", min_length=1, max_length=63)
    ollama_url: str = "http://127.0.0.1:11434"
    provider: Literal["fake", "ollama"] = "fake"
    decision_model: str = Field(default="lfm2.5-thinking:1.2b", min_length=1)
    response_model: str = Field(default="gemma3:1b", min_length=1)
    max_steps: int = Field(default=8, ge=1, le=100)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    max_output_tokens: int = -1
    max_input_bytes: int = Field(default=16384, ge=256, le=131072)
    tool_allowlist: tuple[str, ...] = (
        "calculator",
        "list_files",
        "read_file",
        "read_file_range",
        "write_file",
        "search_files",
        "edit_file",
        "delegate_tasks",
    )
    max_tool_calls: int = Field(default=8, ge=1, le=100)
    repeated_action_limit: int = Field(default=2, ge=1, le=10)
    require_write_approval: bool = False
    required_tools: tuple[str, ...] = ()
    routing_mode: Literal["auto", "chat", "tools", "two_stage", "router_worker"] = "auto"
    response_max_tokens: int = -1
    max_total_steps: int = Field(default=256, ge=1, le=4096)
    max_total_tokens: int = Field(default=262144, ge=1, le=10_000_000)
    max_total_tool_calls: int = Field(default=128, ge=1, le=4096)
    retry_strategy: Literal["none", "transient_provider"] = "none"
    max_retries: int = Field(default=0, ge=0, le=3)
    max_corrections: int = Field(default=1, ge=0, le=3)
    partial_result_policy: Literal["fail", "allow"] = "fail"
    result_schema: dict[str, object] = Field(default_factory=dict)
    memory_retrieval_enabled: bool = False
    memory_extraction_enabled: bool = False
    memory_retrieval_limit: int = Field(default=5, ge=1, le=20)
    memory_context_bytes: int = Field(default=2048, ge=128, le=16384)
    auto_kernel_enabled: bool = True
    auto_kernel_trigger_bytes: int = Field(default=8192, ge=1024, le=131072)
    model_concurrency: int = Field(default=2, ge=1, le=32)
    model_queue_capacity: int = Field(default=8, ge=0, le=1024)
    model_provider_concurrency: int = Field(default=2, ge=1, le=32)
    tool_concurrency: int = Field(default=4, ge=1, le=64)
    tool_queue_capacity: int = Field(default=16, ge=0, le=1024)
    memory_concurrency: int = Field(default=2, ge=1, le=32)
    memory_queue_capacity: int = Field(default=8, ge=0, le=1024)
    blocking_tool_workers: int = Field(default=2, ge=1, le=32)
    blocking_tool_queue_capacity: int = Field(default=8, ge=0, le=1024)
    task_concurrency: int = Field(default=4, ge=1, le=32)
    task_queue_capacity: int = Field(default=16, ge=0, le=1024)

    @field_validator("mongodb_uri")
    @classmethod
    def valid_mongodb_uri(cls, value: SecretStr) -> SecretStr:
        uri = value.get_secret_value()
        if not uri.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MongoDB URI must use mongodb:// or mongodb+srv://")
        return value

    @field_validator("database_name")
    @classmethod
    def valid_database_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", value):
            raise ValueError("MongoDB database name contains unsupported characters")
        return value

    @field_validator("max_output_tokens", "response_max_tokens")
    @classmethod
    def valid_output_tokens(cls, value: int) -> int:
        if value == 0 or value < -1:
            raise ValueError("Output token setting must be positive or -1 for no per-request cap")
        return value

    @field_validator("result_schema")
    @classmethod
    def valid_result_schema(cls, value: dict[str, object]) -> dict[str, object]:
        if value:
            try:
                Draft202012Validator.check_schema(value)
            except SchemaError:
                raise ValueError("Configured result schema is invalid") from None
        return value

    def run_options(self) -> RunOptions:
        return RunOptions(
            result_schema=JsonObject(json.dumps(self.result_schema)),
            **self.model_dump(
                exclude={
                    "mongodb_uri",
                    "database_name",
                    "ollama_url",
                    "model_concurrency",
                    "model_queue_capacity",
                    "model_provider_concurrency",
                    "tool_concurrency",
                    "tool_queue_capacity",
                    "memory_concurrency",
                    "memory_queue_capacity",
                    "blocking_tool_workers",
                    "blocking_tool_queue_capacity",
                    "task_concurrency",
                    "task_queue_capacity",
                    "result_schema",
                }
            ),
        )


def load_settings(path: Path | None = None, **overrides: object) -> Settings:
    try:
        load_dotenv(override=False)
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
