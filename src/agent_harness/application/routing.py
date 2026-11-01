"""Cheap, deterministic routing; explicit mode overrides ambiguous language."""

import re

from agent_harness.domain.errors import require
from agent_harness.domain.model import RunOptions


def infer_required_tool(task: str) -> str | None:
    text = task.lower()
    file_reference = re.search(
        r"\b(?:files?|folders?|directories|workspace|readme(?:\.\w+)?|"
        r"[\w.-]+\.(?:md|txt|json|csv|py|toml|ya?ml))\b",
        text,
    )
    if file_reference is None:
        return None
    if re.search(r"\b(list|enumerate)\b", text) and re.search(
        r"\b(files?|folders?|directories|workspace)\b", text
    ):
        return "list_files"
    if re.search(r"\b(search|grep)\b", text) or (
        re.search(r"\bfind\b", text) and re.search(r"\b(for|inside|within)\b", text)
    ):
        return "search_files"
    if re.search(r"\b(edit|replace|update)\b", text):
        return "edit_file"
    if re.search(r"\b(write|create|save)\b", text):
        return "write_file"
    if re.search(r"\b(read|open|show|display|view|summari[sz]e)\b", text):
        if re.search(r"\blines?\s+\d+\b", text):
            return "read_file_range"
        return "read_file"
    return None


def select_mode(task: str, options: RunOptions) -> str:
    if options.required_tools:
        require(options.routing_mode != "chat", "Chat mode cannot satisfy required tools")
        return "tools"
    if options.routing_mode == "router_worker":
        return "tools"
    if options.routing_mode != "auto":
        return options.routing_mode
    text = task.lower().strip()
    explicit = re.search(
        r"\b(calculator|list_files|read_file_range|"
        r"(read|write|search|edit)_file)\b",
        text,
    )
    command = re.sub(r"^(please\s+|(?:can|could|would) you\s+)", "", text)
    arithmetic = re.match(r"(?:calculate|compute)\b", command)
    filesystem = re.search(
        r"\b(write|save|read|list|create|edit|search|find|replace)\b", command
    ) and re.search(
        r"\b(file|files|workspace)\b|\.(txt|md|json|csv|py|toml|yaml|yml)\b",
        command,
    )
    return "tools" if explicit or arithmetic or filesystem else "chat"
