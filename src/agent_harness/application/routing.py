"""Cheap, deterministic routing; explicit mode overrides ambiguous language."""

import re

from agent_harness.domain.errors import require
from agent_harness.domain.model import RunOptions


def select_mode(task: str, options: RunOptions) -> str:
    if options.required_tools:
        require(options.routing_mode != "chat", "Chat mode cannot satisfy required tools")
        if options.provider == "ollama" and options.routing_mode == "auto":
            return "router_worker"
        return "tools"
    if options.routing_mode != "auto":
        return options.routing_mode
    if options.provider == "ollama":
        return "router_worker"
    text = task.lower().strip()
    explicit = re.search(
        r"\b(calculator|artifact_(write|read|list)|list_files|read_file_range|"
        r"(read|write|search|edit)_file|artifact demo)\b",
        text,
    )
    command = re.sub(r"^(please\s+|(?:can|could|would) you\s+)", "", text)
    arithmetic = re.match(r"(?:calculate|compute)\b", command)
    filesystem = re.search(
        r"\b(write|save|read|list|create|edit|search|find|replace)\b", command
    ) and re.search(
        r"\b(artifact|file|files|workspace)\b|\.(txt|md|json|csv|py|toml|yaml|yml)\b",
        command,
    )
    return "tools" if explicit or arithmetic or filesystem else "chat"
