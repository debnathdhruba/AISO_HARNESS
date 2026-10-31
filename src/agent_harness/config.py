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
    max_steps: int = Field(default=2, ge=1, le=100)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    max_output_tokens: int = Field(default=1024, ge=1, le=8192)
    max_input_bytes: int = Field(default=16384, ge=256, le=131072)

    def run_options(self) -> RunOptions:
        return RunOptions(**self.model_dump(exclude={"database", "ollama_url"}))


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
