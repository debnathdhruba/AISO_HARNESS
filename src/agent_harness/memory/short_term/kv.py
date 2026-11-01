"""Capability-scoped in-memory key/value communication for one kernel execution."""

import asyncio
from collections.abc import Iterable

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.logger import audit


class KernelMemory:
    """Immutable JSON values; only the kernel grants a task access to a key."""

    def __init__(self) -> None:
        self._values: dict[str, JsonObject] = {}
        self._readers: dict[str, frozenset[str]] = {}
        self._lock = asyncio.Lock()

    async def put(self, key: str, value: JsonObject, *, readers: Iterable[str]) -> None:
        reader_set = frozenset(readers)
        require(bool(key.strip()) and bool(reader_set), "Kernel memory key/readers are required")
        async with self._lock:
            require(key not in self._values, "Kernel memory values are immutable")
            self._values[key] = value
            self._readers[key] = reader_set
            audit("kernel.memory.put", key=key, readers=tuple(sorted(reader_set)))

    async def read(self, task_alias: str, key: str) -> JsonObject:
        async with self._lock:
            value = self._values.get(key)
            if value is None or task_alias not in self._readers.get(key, frozenset()):
                raise HarnessError(ErrorCode.AUTHORIZATION, "Kernel memory key is unavailable")
            audit("kernel.memory.read", key=key, reader=task_alias)
            return value

    async def keys_for(self, task_alias: str) -> tuple[str, ...]:
        async with self._lock:
            return tuple(key for key, readers in self._readers.items() if task_alias in readers)
