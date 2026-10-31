"""MongoDB persistence with scoped sessions and atomic transactional state changes."""

import asyncio
import json
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Concatenate, ParamSpec, TypeVar
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.errors import DuplicateKeyError, PyMongoError

from agent_harness.domain.budgets import BudgetLimits, BudgetSnapshot, BudgetUsage
from agent_harness.domain.entities import Checkpoint, Event, JsonObject, Run, Task
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.memory import MemoryRecord, MemorySharing
from agent_harness.domain.model import ModelResponse, Step, StepStatus
from agent_harness.domain.states import (
    RUN_TRANSITIONS,
    ControlIntent,
    InvocationStatus,
    RunStatus,
    TaskStatus,
    validate_transition,
)
from agent_harness.domain.tooling import Approval, ArtifactRecord, Effect, Invocation
from agent_harness.models.codec import decode_step, encode_step
from agent_harness.persistence.codec import decode_run, decode_task, encode
from agent_harness.persistence.tool_codec import (
    decode_artifact,
    decode_invocation,
    encode_artifact,
    encode_invocation,
)
from agent_harness.ports.state import Clock, IdGenerator, StateTransaction

Document = dict[str, Any]
P = ParamSpec("P")
V = TypeVar("V")

RUN_JSON_PATHS = (
    ("data", "config", "model"),
    ("data", "config", "policy"),
    ("data", "config", "budgets"),
    ("data", "config", "memory"),
    ("data", "config", "workspace"),
    ("data", "result"),
    ("data", "error"),
)
TASK_JSON_PATHS = (
    ("data", "acceptance_criteria"),
    ("data", "result"),
    ("data", "handoff"),
    ("data", "error"),
)
STEP_JSON_PATHS = (
    ("data", "request", "messages", "*", "tool_calls", "*", "arguments"),
    ("data", "request", "tools", "*", "input_schema"),
    ("data", "request", "tools", "*", "output_schema"),
    ("data", "response", "message", "tool_calls", "*", "arguments"),
    ("data", "error"),
)
INVOCATION_JSON_PATHS = (("arguments",), ("result",), ("error",))
OWNERSHIP_LEASE_ID = "agent-harness-scheduler"
OWNERSHIP_LEASE_SECONDS = 30


def _update_path(value: Any, path: tuple[str, ...], transform: Callable[[Any], Any]) -> Any:
    if not path:
        return transform(value)
    key, *remaining = path
    if key == "*":
        if isinstance(value, list):
            return [_update_path(item, tuple(remaining), transform) for item in value]
        return value
    if isinstance(value, dict) and key in value and value[key] is not None:
        value[key] = _update_path(value[key], tuple(remaining), transform)
    return value


def _native_snapshot(encoded: str, paths: tuple[tuple[str, ...], ...]) -> Document:
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise HarnessError(ErrorCode.STORAGE, "Invalid persisted snapshot")
    for path in paths:
        value = _update_path(
            value,
            path,
            lambda field: (
                json.loads(field["text"])
                if isinstance(field, dict) and set(field) == {"text"}
                else field
            ),
        )
    if not isinstance(value, dict):
        raise HarnessError(ErrorCode.STORAGE, "Invalid persisted snapshot")
    return value


def _snapshot_text(value: str | Document, paths: tuple[tuple[str, ...], ...]) -> str:
    if isinstance(value, str):
        return value
    for path in paths:
        value = _update_path(
            value,
            path,
            lambda field: {"text": json.dumps(field, sort_keys=True, separators=(",", ":"))},
        )
    return json.dumps(value, allow_nan=False, sort_keys=True)


def _stored_json_text(value: str | Document) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def conflict() -> HarnessError:
    return HarnessError(ErrorCode.CONCURRENCY_CONFLICT, "State version changed", retryable=True)


def mutation(
    operation: Callable[Concatenate["MongoTransaction", P], Awaitable[V]],
) -> Callable[Concatenate["MongoTransaction", P], Coroutine[Any, Any, V]]:
    @wraps(operation)
    async def guarded(tx: "MongoTransaction", /, *args: P.args, **kwargs: P.kwargs) -> V:
        tx.check_active()
        try:
            return await operation(tx, *args, **kwargs)
        except PyMongoError:
            tx.failed = True
            raise HarnessError(ErrorCode.STORAGE, "MongoDB operation failed") from None
        except BaseException:
            tx.failed = True
            raise

    return guarded


class MongoStore:
    """Application-owned async Mongo client; transactions are isolated per unit of work."""

    def __init__(self, uri: str, database_name: str, clock: Clock, ids: IdGenerator) -> None:
        require(bool(uri.strip()), "MongoDB URI is required")
        require(bool(database_name.strip()), "MongoDB database name is required")
        self._client: AsyncMongoClient[Document] = AsyncMongoClient(
            uri, serverSelectionTimeoutMS=5000
        )
        self._database = self._client[database_name]
        self._clock, self._ids = clock, ids
        self._opened = False
        self._active_transactions = 0
        self._lifecycle_lock = asyncio.Lock()
        self._transaction_lock = asyncio.Lock()
        self._transaction_owner: asyncio.Task[object] | None = None
        self._scheduler_owner_id: str | None = None
        self._scheduler_lease_lost = asyncio.Event()
        self._scheduler_heartbeat: asyncio.Task[None] | None = None

    @staticmethod
    def _memory_document(record: MemoryRecord) -> Document:
        return {
            "_id": record.record_id,
            "record_id": record.record_id,
            "user_id": record.user_id,
            "owner_agent_definition_id": record.owner_agent_definition_id,
            "content": record.content,
            "tags": list(record.tags),
            "sharing": record.sharing.value,
            "source_run_id": record.source_run_id,
            "source_task_id": record.source_task_id,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "deleted_at": record.deleted_at.isoformat() if record.deleted_at else None,
        }

    @staticmethod
    def _decode_memory(document: Document) -> MemoryRecord:
        return MemoryRecord(
            record_id=str(document["record_id"]),
            user_id=str(document["user_id"]),
            owner_agent_definition_id=str(document["owner_agent_definition_id"]),
            content=str(document["content"]),
            tags=tuple(str(tag) for tag in document["tags"]),
            sharing=MemorySharing(str(document["sharing"])),
            created_at=datetime.fromisoformat(str(document["created_at"])),
            updated_at=datetime.fromisoformat(str(document["updated_at"])),
            source_run_id=(
                str(document["source_run_id"]) if document.get("source_run_id") else None
            ),
            source_task_id=(
                str(document["source_task_id"]) if document.get("source_task_id") else None
            ),
            deleted_at=(
                datetime.fromisoformat(str(document["deleted_at"]))
                if document.get("deleted_at")
                else None
            ),
        )

    async def __aenter__(self) -> "MongoStore":
        async with self._lifecycle_lock:
            require(not self._opened, "Mongo store is already open")
            try:
                await self._client.admin.command("ping")
                await self._ensure_indexes()
            except PyMongoError:
                await self._client.close()
                raise HarnessError(ErrorCode.STORAGE, "Cannot connect to MongoDB") from None
            self._opened = True
        return self

    async def __aexit__(self, *_: object) -> None:
        require(
            self._transaction_owner is not asyncio.current_task(),
            "Cannot close MongoDB inside a transaction",
        )
        async with self._transaction_lock:
            async with self._lifecycle_lock:
                require(
                    self._active_transactions == 0,
                    "Cannot close MongoDB during a transaction",
                )
                if self._opened:
                    self._opened = False
                    await self._client.close()

    async def save_memory(self, record: MemoryRecord) -> None:
        require(self._opened, "Mongo store is not open")
        try:
            await self._validate_memory_provenance(record)
            document = self._memory_document(record)
            identity = {
                key: value
                for key, value in document.items()
                if key not in {"_id", "created_at", "updated_at"}
            }
            await self._database["memory_records"].update_one(
                {"_id": record.record_id, **identity},
                {"$setOnInsert": document},
                upsert=True,
            )
        except DuplicateKeyError:
            raise HarnessError(
                ErrorCode.CONCURRENCY_CONFLICT,
                "Memory id was already used for different content",
            ) from None
        except PyMongoError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot save memory record") from None

    async def _validate_memory_provenance(self, record: MemoryRecord) -> None:
        if record.source_run_id is not None:
            run = await self._database["runs"].find_one(
                {"run_id": record.source_run_id, "user_id": record.user_id},
                projection={"_id": 1},
            )
            require(run is not None, "Memory run provenance is unavailable")
        if record.source_task_id is not None:
            row = await self._database["tasks"].find_one(
                {
                    "task_id": record.source_task_id,
                    "run_id": record.source_run_id,
                    "user_id": record.user_id,
                }
            )
            require(row is not None, "Memory task provenance is unavailable")
            assert row is not None
            task = decode_task(_snapshot_text(row["snapshot"], TASK_JSON_PATHS))
            require(
                task.agent_definition_id == record.owner_agent_definition_id,
                "Memory owner does not match task provenance",
            )

    async def get_memory(
        self, record_id: str, user_id: str, agent_definition_id: str
    ) -> MemoryRecord | None:
        require(self._opened, "Mongo store is not open")
        try:
            document = await self._database["memory_records"].find_one(
                {
                    "_id": record_id,
                    "user_id": user_id,
                    "deleted_at": None,
                    "$or": [
                        {"sharing": MemorySharing.SHARED.value},
                        {
                            "sharing": MemorySharing.PRIVATE.value,
                            "owner_agent_definition_id": agent_definition_id,
                        },
                    ],
                }
            )
        except PyMongoError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot read memory record") from None
        return self._decode_memory(document) if document is not None else None

    async def update_memory(self, record: MemoryRecord, expected_updated_at: str) -> None:
        require(self._opened, "Mongo store is not open")
        document = self._memory_document(record)
        document.pop("_id")
        try:
            result = await self._database["memory_records"].update_one(
                {
                    "_id": record.record_id,
                    "user_id": record.user_id,
                    "owner_agent_definition_id": record.owner_agent_definition_id,
                    "deleted_at": None,
                    "updated_at": expected_updated_at,
                },
                {"$set": document},
            )
        except PyMongoError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot update memory record") from None
        if result.matched_count != 1:
            raise HarnessError(
                ErrorCode.CONCURRENCY_CONFLICT,
                "Memory record changed or is unavailable",
                retryable=True,
            )

    async def delete_memory(
        self, record_id: str, user_id: str, agent_definition_id: str, deleted_at: str
    ) -> None:
        require(self._opened, "Mongo store is not open")
        try:
            result = await self._database["memory_records"].update_one(
                {
                    "_id": record_id,
                    "user_id": user_id,
                    "owner_agent_definition_id": agent_definition_id,
                    "deleted_at": None,
                },
                {"$set": {"deleted_at": deleted_at, "updated_at": deleted_at}},
            )
        except PyMongoError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot delete memory record") from None
        if result.matched_count != 1:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Memory record is unavailable")

    async def memory_candidates(
        self, user_id: str, agent_definition_id: str, limit: int
    ) -> tuple[MemoryRecord, ...]:
        require(self._opened, "Mongo store is not open")
        require(1 <= limit <= 500, "Invalid memory candidate limit")
        try:
            cursor = (
                self._database["memory_records"]
                .find(
                    {
                        "user_id": user_id,
                        "deleted_at": None,
                        "$or": [
                            {"sharing": MemorySharing.SHARED.value},
                            {
                                "sharing": MemorySharing.PRIVATE.value,
                                "owner_agent_definition_id": agent_definition_id,
                            },
                        ],
                    }
                )
                .sort([("updated_at", DESCENDING), ("_id", ASCENDING)])
                .limit(limit)
            )
            documents = [document async for document in cursor]
        except PyMongoError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot retrieve memory candidates") from None
        return tuple(self._decode_memory(document) for document in documents)

    @asynccontextmanager
    async def scheduler_ownership(self) -> AsyncGenerator[None, None]:
        require(self._opened, "Mongo store is not open")
        require(self._scheduler_owner_id is None, "Scheduler ownership is already held")
        owner_id = str(uuid4())
        now = self._clock.utc_now()
        try:
            lease = await self._database["process_ownership"].find_one_and_update(
                {
                    "_id": OWNERSHIP_LEASE_ID,
                    "$or": [
                        {"expires_at": {"$lte": now}},
                        {"owner_id": owner_id},
                    ],
                },
                {
                    "$set": {
                        "owner_id": owner_id,
                        "process_id": os.getpid(),
                        "heartbeat_at": now,
                        "expires_at": now + timedelta(seconds=OWNERSHIP_LEASE_SECONDS),
                    }
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            raise HarnessError(
                ErrorCode.CONCURRENCY_CONFLICT,
                "Another process owns the agent scheduler",
                retryable=True,
            ) from None
        except PyMongoError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot acquire scheduler ownership") from None
        require(
            lease is not None and lease.get("owner_id") == owner_id,
            "Another process owns the agent scheduler",
        )
        self._scheduler_owner_id = owner_id
        self._scheduler_lease_lost.clear()
        self._scheduler_heartbeat = asyncio.create_task(self._renew_scheduler_lease(owner_id))
        try:
            await self.recover_startup()
            yield
        finally:
            heartbeat = self._scheduler_heartbeat
            self._scheduler_heartbeat = None
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            self._scheduler_owner_id = None
            await self._database["process_ownership"].delete_one(
                {"_id": OWNERSHIP_LEASE_ID, "owner_id": owner_id}
            )

    async def _renew_scheduler_lease(self, owner_id: str) -> None:
        while True:
            await asyncio.sleep(OWNERSHIP_LEASE_SECONDS / 3)
            now = self._clock.utc_now()
            try:
                async with self._transaction_lock:
                    result = await self._database["process_ownership"].update_one(
                        {
                            "_id": OWNERSHIP_LEASE_ID,
                            "owner_id": owner_id,
                            "expires_at": {"$gt": now},
                        },
                        {
                            "$set": {
                                "heartbeat_at": now,
                                "expires_at": now + timedelta(seconds=OWNERSHIP_LEASE_SECONDS),
                            }
                        },
                    )
            except PyMongoError:
                self._scheduler_lease_lost.set()
                return
            if result.matched_count != 1:
                self._scheduler_lease_lost.set()
                return

    async def assert_scheduler_ownership(self) -> None:
        owner_id = self._scheduler_owner_id
        require(owner_id is not None, "This process does not own the agent scheduler")
        require(not self._scheduler_lease_lost.is_set(), "Scheduler ownership lease was lost")
        now = self._clock.utc_now()
        try:
            lease = await self._database["process_ownership"].find_one(
                {
                    "_id": OWNERSHIP_LEASE_ID,
                    "owner_id": owner_id,
                    "expires_at": {"$gt": now},
                },
                projection={"_id": 1},
            )
        except PyMongoError:
            raise HarnessError(ErrorCode.STORAGE, "Cannot verify scheduler ownership") from None
        require(lease is not None, "Scheduler ownership lease was lost")

    async def recover_startup(self) -> None:
        require(
            self._scheduler_owner_id is not None, "Scheduler ownership is required for recovery"
        )
        cursor = self._database["runs"].find(
            {
                "status": {
                    "$in": [
                        RunStatus.QUEUED.value,
                        RunStatus.RUNNING.value,
                        RunStatus.WAITING.value,
                        RunStatus.PAUSED.value,
                    ]
                }
            },
            projection={"run_id": 1, "user_id": 1},
        )
        records = [record async for record in cursor]
        for record in records:
            await self.assert_scheduler_ownership()
            async with self.transaction(str(record["user_id"])) as tx:
                assert isinstance(tx, MongoTransaction)
                await tx.recover_run(str(record["run_id"]))
        await self.assert_scheduler_ownership()

    async def _ensure_indexes(self) -> None:
        definitions: dict[str, tuple[tuple[str, int], ...]] = {
            "runs": (("user_id", ASCENDING), ("status", ASCENDING)),
            "tasks": (
                ("user_id", ASCENDING),
                ("run_id", ASCENDING),
                ("status", ASCENDING),
            ),
            "task_dependencies": (
                ("task_id", ASCENDING),
                ("dependency_id", ASCENDING),
                ("run_id", ASCENDING),
                ("user_id", ASCENDING),
            ),
            "events": (("run_id", ASCENDING), ("sequence", ASCENDING)),
            "checkpoints": (("run_id", ASCENDING), ("user_id", ASCENDING)),
            "steps": (
                ("task_id", ASCENDING),
                ("ordinal", ASCENDING),
            ),
            "invocations": (
                ("step_id", ASCENDING),
                ("call_id", ASCENDING),
            ),
            "approvals": (("user_id", ASCENDING),),
            "artifacts": (
                ("user_id", ASCENDING),
                ("run_id", ASCENDING),
                ("task_id", ASCENDING),
                ("name", ASCENDING),
                ("version", ASCENDING),
            ),
            "delegations": (
                ("parent_task_id", ASCENDING),
                ("idempotency_key", ASCENDING),
            ),
            "budget_reservations": (
                ("run_id", ASCENDING),
                ("task_id", ASCENDING),
                ("status", ASCENDING),
            ),
            "budget_accounts": (
                ("run_id", ASCENDING),
                ("user_id", ASCENDING),
            ),
            "memory_records": (
                ("user_id", ASCENDING),
                ("deleted_at", ASCENDING),
                ("sharing", ASCENDING),
                ("owner_agent_definition_id", ASCENDING),
                ("updated_at", DESCENDING),
            ),
        }
        for collection_name, keys in definitions.items():
            collection = self._database[collection_name]
            await collection.create_index(
                list(keys),
                unique=collection_name
                in {
                    "task_dependencies",
                    "steps",
                    "artifacts",
                    "delegations",
                    "events",
                    "invocations",
                },
            )
        for collection_name, field in (
            ("events", "event_id"),
            ("tasks", "task_id"),
            ("runs", "run_id"),
            ("steps", "step_id"),
            ("invocations", "invocation_id"),
            ("approvals", "invocation_id"),
            ("artifacts", "artifact_id"),
            ("checkpoints", "task_id"),
        ):
            await self._database[collection_name].create_index(field, unique=True)
        await self._database["delegations"].create_index("child_task_id", unique=True)
        await self._database["budget_accounts"].create_index("task_id", unique=True)
        await self._database["budget_reservations"].create_index("reservation_id", unique=True)

    @asynccontextmanager
    async def transaction(self, user_id: str) -> AsyncGenerator[StateTransaction, None]:
        require(bool(user_id.strip()), "Transaction principal is required")
        require(
            self._transaction_owner is not asyncio.current_task(),
            "Nested transactions are not supported",
        )
        async with self._transaction_lock:
            require(self._opened, "Mongo store is not open")
            self._transaction_owner = asyncio.current_task()
            async with self._lifecycle_lock:
                self._active_transactions += 1
            tx: MongoTransaction | None = None
            try:
                async with self._client.start_session() as session:
                    async with await session.start_transaction():
                        tx = MongoTransaction(
                            self._database,
                            session,
                            user_id,
                            self._clock,
                            self._ids,
                            self._scheduler_owner_id,
                        )
                        if self._scheduler_owner_id is not None:
                            await tx.ensure_scheduler_fence()
                        try:
                            yield tx
                            tx.check_active()
                            if tx.failed:
                                raise HarnessError(
                                    ErrorCode.STORAGE,
                                    "Transaction must roll back after a failed mutation",
                                )
                        finally:
                            tx.active = False
            except PyMongoError:
                raise HarnessError(ErrorCode.STORAGE, "MongoDB operation failed") from None
            finally:
                if tx is not None:
                    tx.active = False
                async with self._lifecycle_lock:
                    self._active_transactions -= 1
                self._transaction_owner = None


class MongoTransaction:
    def __init__(
        self,
        database: Any,
        session: AsyncClientSession,
        user_id: str,
        clock: Clock,
        ids: IdGenerator,
        scheduler_owner_id: str | None = None,
    ) -> None:
        self._db = database
        self._session = session
        self._user = user_id
        self._clock, self._ids = clock, ids
        self._scheduler_owner_id = scheduler_owner_id
        self._owner = asyncio.current_task()
        self.active = True
        self.failed = False

    def check_active(self) -> None:
        require(
            self.active and self._owner is asyncio.current_task(),
            "Transaction is closed or belongs to another coroutine",
        )

    @mutation
    async def ensure_scheduler_fence(self) -> None:
        owner_id = self._scheduler_owner_id
        require(owner_id is not None, "Scheduler ownership is required")
        now = self._clock.utc_now()
        result = await self._collection("process_ownership").update_one(
            {
                "_id": OWNERSHIP_LEASE_ID,
                "owner_id": owner_id,
                "expires_at": {"$gt": now},
            },
            {
                "$set": {
                    "heartbeat_at": now,
                    "expires_at": now + timedelta(seconds=OWNERSHIP_LEASE_SECONDS),
                }
            },
            session=self._session,
        )
        if result.matched_count != 1:
            raise HarnessError(
                ErrorCode.CONCURRENCY_CONFLICT,
                "Scheduler ownership lease was lost",
                retryable=True,
            )

    def _scope(self, user_id: str) -> None:
        self.check_active()
        if user_id != self._user:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Principal does not own this object")

    def _collection(self, name: str) -> Any:
        self.check_active()
        return self._db[name]

    async def _find(
        self,
        collection: str,
        criteria: Document,
        *,
        sort: tuple[tuple[str, int], ...] = (("_id", ASCENDING),),
        limit: int | None = None,
    ) -> list[Document]:
        cursor = self._collection(collection).find(criteria, session=self._session).sort(list(sort))
        if limit is not None:
            cursor = cursor.limit(limit)
        return [item async for item in cursor]

    async def _task_ancestors(self, task_id: str) -> tuple[Task, ...]:
        current = await self.get_task(task_id)
        require(current is not None, "Budget task is unavailable")
        chain: list[Task] = []
        seen: set[str] = set()
        while current is not None:
            require(current.task_id not in seen, "Task ancestry contains a cycle")
            seen.add(current.task_id)
            chain.append(current)
            if current.parent_task_id is None:
                break
            current = await self.get_task(current.parent_task_id)
            require(
                current is not None and current.run_id == chain[0].run_id,
                "Budget ancestry crosses a run boundary",
            )
        return tuple(chain)

    async def _ensure_budget_account(self, task: Task) -> Document:
        row: Document | None = await self._collection("budget_accounts").find_one(
            {"task_id": task.task_id, "user_id": self._user},
            session=self._session,
        )
        if row is not None:
            return row
        run = await self.get_run(task.run_id)
        require(run is not None, "Budget run is unavailable")
        assert run is not None
        configured = json.loads(run.config.budgets.text)
        if task.parent_task_id is None:
            limits = BudgetLimits(
                int(configured.get("max_total_steps", configured.get("max_steps", 8))),
                int(configured.get("max_total_tokens", 262144)),
                int(configured.get("max_total_tool_calls", 128)),
            )
        else:
            parent_row = await self._collection("budget_accounts").find_one(
                {"task_id": task.parent_task_id, "user_id": self._user},
                session=self._session,
            )
            parent_limits = (
                BudgetLimits(**parent_row["limits"])
                if parent_row is not None
                else BudgetLimits(
                    int(configured.get("max_total_steps", configured.get("max_steps", 8))),
                    int(configured.get("max_total_tokens", 262144)),
                    int(configured.get("max_total_tool_calls", 128)),
                )
            )
            requested_steps = int(
                json.loads(task.handoff.text).get("max_steps", parent_limits.steps)
            )
            limits = BudgetLimits(
                min(requested_steps, parent_limits.steps),
                parent_limits.tokens,
                parent_limits.tool_calls,
            )
        document = {
            "_id": task.task_id,
            "task_id": task.task_id,
            "run_id": task.run_id,
            "user_id": task.user_id,
            "limits": limits.document(),
            "used": {"steps": 0, "tokens": 0, "tool_calls": 0},
            "reserved": {"steps": 0, "tokens": 0, "tool_calls": 0},
            "unknown_usage_count": 0,
        }
        await self._collection("budget_accounts").insert_one(document, session=self._session)
        return document

    @mutation
    async def reserve_budget(self, reservation_id: str, task_id: str, amount: BudgetUsage) -> None:
        self.check_active()
        requested = amount.reservation()
        existing = await self._collection("budget_reservations").find_one(
            {"reservation_id": reservation_id, "user_id": self._user},
            session=self._session,
        )
        if existing is not None:
            require(
                existing["task_id"] == task_id and existing["reserved"] == requested,
                "Budget reservation ID was reused with different work",
            )
            return
        task = await self.get_task(task_id)
        require(task is not None, "Budget task is unavailable")
        assert task is not None
        ancestors = await self._task_ancestors(task_id)
        account_ids = tuple(item.task_id for item in ancestors)
        for ancestor in ancestors:
            account = await self._ensure_budget_account(ancestor)
            limits = account["limits"]
            used = account["used"]
            reserved = account["reserved"]
            if any(
                int(used[key]) + int(reserved[key]) + requested[key] > int(limits[key])
                for key in requested
            ):
                raise HarnessError(
                    ErrorCode.BUDGET_EXCEEDED,
                    "Hierarchical run budget exhausted",
                )
            increments = {f"reserved.{key}": value for key, value in requested.items()}
            result = await self._collection("budget_accounts").update_one(
                {"task_id": ancestor.task_id, "user_id": self._user},
                {"$inc": increments},
                session=self._session,
            )
            if result.matched_count != 1:
                raise conflict()
        await self._collection("budget_reservations").insert_one(
            {
                "_id": reservation_id,
                "reservation_id": reservation_id,
                "run_id": task.run_id,
                "task_id": task_id,
                "user_id": self._user,
                "ancestor_task_ids": list(account_ids),
                "reserved": requested,
                "status": "reserved",
                "actual": None,
                "charged": None,
                "usage_unknown": False,
            },
            session=self._session,
        )
        await self._event(
            task.run_id,
            "budget.reserved",
            task_id=task_id,
            details=JsonObject(json.dumps({"reservation_id": reservation_id, "units": requested})),
        )

    @mutation
    async def reconcile_budget(self, reservation_id: str, actual: BudgetUsage) -> None:
        row = await self._collection("budget_reservations").find_one(
            {"reservation_id": reservation_id, "user_id": self._user},
            session=self._session,
        )
        require(row is not None, "Budget reservation is unavailable")
        assert row is not None
        supplied = actual.document()
        if row["status"] == "reconciled":
            require(row["actual"] == supplied, "Budget reconciliation cannot be changed")
            return
        charged = {
            key: int(row["reserved"][key]) if value is None else value
            for key, value in supplied.items()
        }
        usage_unknown = any(value is None for value in supplied.values())
        for ancestor_id in row["ancestor_task_ids"]:
            result = await self._collection("budget_accounts").update_one(
                {"task_id": ancestor_id, "user_id": self._user},
                {
                    "$inc": {
                        **{
                            f"reserved.{key}": -int(value) for key, value in row["reserved"].items()
                        },
                        **{f"used.{key}": value for key, value in charged.items()},
                        **({"unknown_usage_count": 1} if usage_unknown else {}),
                    }
                },
                session=self._session,
            )
            if result.matched_count != 1:
                raise HarnessError(ErrorCode.STORAGE, "Budget account is unavailable")
        await self._collection("budget_reservations").update_one(
            {"reservation_id": reservation_id, "user_id": self._user, "status": "reserved"},
            {
                "$set": {
                    "status": "reconciled",
                    "actual": supplied,
                    "charged": charged,
                    "usage_unknown": usage_unknown,
                }
            },
            session=self._session,
        )
        await self._event(
            row["run_id"],
            "budget.reconciled",
            task_id=row["task_id"],
            details=JsonObject(
                json.dumps(
                    {
                        "reservation_id": reservation_id,
                        "actual": supplied,
                        "charged": charged,
                        "usage_unknown": usage_unknown,
                    }
                )
            ),
        )

    @mutation
    async def budget_snapshot(self, task_id: str) -> BudgetSnapshot:
        task = await self.get_task(task_id)
        require(task is not None, "Budget task is unavailable")
        assert task is not None
        row = await self._ensure_budget_account(task)
        return BudgetSnapshot(
            BudgetLimits(**row["limits"]),
            BudgetUsage(**row["used"]),
            BudgetUsage(**row["reserved"]),
            int(row["unknown_usage_count"]),
        )

    @mutation
    async def recover_run(self, run_id: str) -> None:
        run = await self.get_run(run_id)
        if run is None or not RUN_TRANSITIONS[run.status]:
            return
        tasks = await self.tasks(run_id)
        task_map = {task.task_id: task for task in tasks}
        root_task_id = str(json.loads(run.config.policy.text).get("root_task_id", ""))
        invalid_tree = root_task_id not in task_map or (
            root_task_id in task_map and task_map[root_task_id].parent_task_id is not None
        )
        for task in tasks:
            if task.parent_task_id is not None:
                parent = task_map.get(task.parent_task_id)
                invalid_tree = invalid_tree or parent is None or parent.depth + 1 != task.depth
            invalid_tree = invalid_tree or any(
                dependency not in task_map for dependency in task.dependencies
            )
            ancestry: set[str] = set()
            current = task
            while current.parent_task_id is not None:
                if current.task_id in ancestry:
                    invalid_tree = True
                    break
                ancestry.add(current.task_id)
                parent = task_map.get(current.parent_task_id)
                if parent is None:
                    invalid_tree = True
                    break
                current = parent
        dependency_graph = {task.task_id: task.dependencies for task in tasks}
        checked: set[str] = set()
        visiting: set[str] = set()

        def visit(task_id: str) -> None:
            nonlocal invalid_tree
            if task_id in visiting:
                invalid_tree = True
                return
            if task_id in checked:
                return
            visiting.add(task_id)
            for dependency_id in dependency_graph.get(task_id, ()):
                visit(dependency_id)
            visiting.remove(task_id)
            checked.add(task_id)

        for task_id in dependency_graph:
            visit(task_id)
        if invalid_tree:
            for task in await self.tasks(run_id):
                if task.status in {
                    TaskStatus.PENDING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.WAITING_CHILDREN,
                    TaskStatus.WAITING_APPROVAL,
                    TaskStatus.PAUSED,
                }:
                    await self.transition_task(
                        task.task_id,
                        task.version,
                        TaskStatus.FAILED,
                        error=JsonObject(
                            json.dumps(
                                {
                                    "code": ErrorCode.STORAGE.value,
                                    "message": "Persisted task tree failed recovery validation",
                                }
                            )
                        ),
                    )
            await self.transition_run(
                run_id,
                run.version,
                RunStatus.FAILED,
                error=JsonObject(
                    json.dumps(
                        {
                            "code": ErrorCode.STORAGE.value,
                            "message": "Persisted task tree failed recovery validation",
                        }
                    )
                ),
            )
            await self._event(
                run_id,
                "recovery.rejected",
                details=JsonObject('{"reason":"invalid_task_tree"}'),
            )
            return

        reservations = await self._find(
            "budget_reservations",
            {"run_id": run_id, "user_id": self._user, "status": "reserved"},
            sort=(("reservation_id", ASCENDING),),
        )
        for reservation in reservations:
            await self.reconcile_budget(
                str(reservation["reservation_id"]),
                BudgetUsage(steps=None, tokens=None, tool_calls=None),
            )

        for step in await self.steps(run_id):
            if step.status == StepStatus.REQUESTED:
                await self.update_step(
                    step.step_id,
                    StepStatus.REQUESTED,
                    StepStatus.FAILED,
                    error=JsonObject(
                        json.dumps(
                            {
                                "code": ErrorCode.TRANSIENT_PROVIDER.value,
                                "message": (
                                    "Model attempt was interrupted before response acceptance"
                                ),
                                "retryable": True,
                                "recovery_retry": True,
                            }
                        )
                    ),
                )

        for invocation in await self.invocations(run_id):
            if invocation.status != InvocationStatus.EXECUTING:
                continue
            status = (
                InvocationStatus.UNKNOWN
                if invocation.effect == Effect.NON_IDEMPOTENT_WRITE
                else InvocationStatus.READY
            )
            await self.recover_invocation(invocation.invocation_id, status)

        run = await self.get_run(run_id)
        assert run is not None
        if run.deadline is not None and run.deadline <= self._clock.utc_now():
            for task in await self.tasks(run_id):
                if task.status in {
                    TaskStatus.PENDING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.WAITING_CHILDREN,
                    TaskStatus.WAITING_APPROVAL,
                    TaskStatus.PAUSED,
                }:
                    await self.transition_task(
                        task.task_id,
                        task.version,
                        TaskStatus.FAILED,
                        error=JsonObject(
                            json.dumps(
                                {
                                    "code": ErrorCode.DEADLINE_EXCEEDED.value,
                                    "message": "Run deadline expired during recovery",
                                }
                            )
                        ),
                    )
            await self.transition_run(
                run_id,
                run.version,
                RunStatus.FAILED,
                error=JsonObject(
                    json.dumps(
                        {
                            "code": ErrorCode.DEADLINE_EXCEEDED.value,
                            "message": "Run deadline expired during recovery",
                        }
                    )
                ),
            )
            await self._event(
                run_id,
                "recovery.expired",
                details=JsonObject('{"decision":"failed"}'),
            )
            return

        if run.intent == ControlIntent.CANCEL:
            for task in await self.tasks(run_id):
                if task.status in {
                    TaskStatus.PENDING,
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.WAITING_CHILDREN,
                    TaskStatus.WAITING_APPROVAL,
                    TaskStatus.PAUSED,
                }:
                    await self.transition_task(
                        task.task_id,
                        task.version,
                        TaskStatus.CANCELLED,
                        error=JsonObject(
                            json.dumps(
                                {
                                    "code": ErrorCode.CANCELLATION.value,
                                    "message": "Run cancellation completed during recovery",
                                }
                            )
                        ),
                    )
            await self.transition_run(
                run_id,
                run.version,
                RunStatus.CANCELLED,
                error=JsonObject(
                    json.dumps(
                        {
                            "code": ErrorCode.CANCELLATION.value,
                            "message": "Run cancellation completed during recovery",
                        }
                    )
                ),
            )
            await self._event(
                run_id,
                "recovery.cancelled",
                details=JsonObject('{"decision":"cancelled"}'),
            )
            return

        should_pause = run.status in {RunStatus.QUEUED, RunStatus.RUNNING} or (
            run.status == RunStatus.WAITING and run.intent == ControlIntent.PAUSE
        )
        should_pause = should_pause or run.status == RunStatus.PAUSED
        if should_pause:
            if run.intent == ControlIntent.NONE:
                run = await self.set_intent(run_id, run.version, ControlIntent.PAUSE)
            for task in await self.tasks(run_id):
                if task.status not in {
                    TaskStatus.READY,
                    TaskStatus.RUNNING,
                    TaskStatus.WAITING_CHILDREN,
                    TaskStatus.WAITING_APPROVAL,
                }:
                    continue
                paused_from = task.status.value
                paused = await self.transition_task(task.task_id, task.version, TaskStatus.PAUSED)
                await self._save_recovery_checkpoint(
                    paused,
                    paused_from=paused_from,
                    reason="startup_recovery",
                )
            run = await self.get_run(run_id)
            assert run is not None
            if run.status != RunStatus.PAUSED:
                run = await self.transition_run(run_id, run.version, RunStatus.PAUSED)
            await self._event(
                run_id,
                "recovery.paused",
                details=JsonObject('{"decision":"inspect_then_resume"}'),
            )
        elif run.status == RunStatus.WAITING:
            await self._event(
                run_id,
                "recovery.waiting",
                details=JsonObject('{"decision":"preserved_waiting_state"}'),
            )

    async def _save_recovery_checkpoint(self, task: Task, *, paused_from: str, reason: str) -> None:
        previous = await self.load_checkpoint(task.task_id)
        continuation = json.loads(previous.continuation.text) if previous is not None else {}
        continuation["recovery"] = {"paused_from": paused_from, "reason": reason}
        latest = await self._find(
            "events",
            {"run_id": task.run_id, "task_id": task.task_id, "user_id": self._user},
            sort=(("sequence", DESCENDING),),
            limit=1,
        )
        run_latest = await self._find(
            "events",
            {"run_id": task.run_id, "user_id": self._user},
            sort=(("sequence", DESCENDING),),
            limit=1,
        )
        sequence = int(run_latest[0]["sequence"]) if run_latest else 0
        task_sequence = int(latest[0]["sequence"]) if latest else 0
        await self.save_checkpoint(
            Checkpoint(
                task.task_id,
                task.run_id,
                task.user_id,
                task.version,
                max(sequence, task_sequence),
                JsonObject(json.dumps(continuation)),
                version=0 if previous is None else previous.version + 1,
                schema_version=2,
            ),
            None if previous is None else previous.version,
        )

    @mutation
    async def recover_invocation(self, invocation_id: str, status: InvocationStatus) -> Invocation:
        invocation = await self.get_invocation(invocation_id)
        require(
            invocation is not None and invocation.status == InvocationStatus.EXECUTING,
            "Only an interrupted executing invocation can be recovered",
        )
        assert invocation is not None
        require(
            status
            == (
                InvocationStatus.UNKNOWN
                if invocation.effect == Effect.NON_IDEMPOTENT_WRITE
                else InvocationStatus.READY
            ),
            "Recovery status does not match the invocation effect",
        )
        updated = replace(
            invocation,
            status=status,
            error=(
                JsonObject(
                    json.dumps(
                        {
                            "code": ErrorCode.UNCERTAIN_SIDE_EFFECT.value,
                            "message": (
                                "Interrupted non-idempotent action requires explicit reconciliation"
                            ),
                        }
                    )
                )
                if status == InvocationStatus.UNKNOWN
                else None
            ),
        )
        await self.save_invocation(updated, InvocationStatus.EXECUTING)
        return updated

    @mutation
    async def reconcile_invocation(
        self,
        invocation_id: str,
        *,
        succeeded: bool,
        result: JsonObject | None = None,
        error: JsonObject | None = None,
    ) -> Invocation:
        invocation = await self.get_invocation(invocation_id)
        require(
            invocation is not None and invocation.status == InvocationStatus.UNKNOWN,
            "Invocation is not awaiting reconciliation",
        )
        assert invocation is not None
        run = await self.get_run(invocation.run_id)
        require(
            run is not None
            and run.status == RunStatus.PAUSED
            and run.intent == ControlIntent.PAUSE,
            "Run must be paused before reconciling an uncertain invocation",
        )
        require(
            (succeeded and result is not None and error is None)
            or (not succeeded and error is not None and result is None),
            "Provide exactly one outcome: a result for success or an error for failure",
        )
        updated = replace(
            invocation,
            status=InvocationStatus.SUCCEEDED if succeeded else InvocationStatus.FAILED,
            result=result,
            error=error,
        )
        await self.save_invocation(updated, InvocationStatus.UNKNOWN)
        return updated

    @mutation
    async def record_completion_decision(
        self, run_id: str, task_id: str, decision: JsonObject
    ) -> None:
        task = await self.get_task(task_id)
        require(task is not None and task.run_id == run_id, "Completion task is unavailable")
        await self._event(
            run_id,
            "completion.validated",
            task_id=task_id,
            details=decision,
        )

    async def invocations(self, run_id: str) -> tuple[Invocation, ...]:
        return tuple(
            decode_invocation(_snapshot_text(row["snapshot"], INVOCATION_JSON_PATHS))
            for row in await self._find("invocations", {"run_id": run_id, "user_id": self._user})
        )

    async def get_invocation(self, invocation_id: str) -> Invocation | None:
        row = await self._collection("invocations").find_one(
            {"invocation_id": invocation_id, "user_id": self._user},
            session=self._session,
        )
        return (
            decode_invocation(_snapshot_text(row["snapshot"], INVOCATION_JSON_PATHS))
            if row
            else None
        )

    @mutation
    async def save_invocation(
        self, invocation: Invocation, expected: InvocationStatus | None
    ) -> None:
        self._scope(invocation.user_id)
        previous = await self.get_invocation(invocation.invocation_id)
        if (previous.status if previous else None) != expected:
            raise conflict()
        if previous is None:
            require(
                invocation.status == InvocationStatus.PLANNED,
                "Invocation must start planned",
            )
            step_records = await self.steps(invocation.run_id, invocation.task_id)
            require(
                any(
                    step.step_id == invocation.step_id and step.status == StepStatus.ACCEPTED
                    for step in step_records
                ),
                "Invocation requires an accepted decision",
            )
            await self._collection("invocations").insert_one(
                {
                    "_id": invocation.invocation_id,
                    "invocation_id": invocation.invocation_id,
                    "run_id": invocation.run_id,
                    "task_id": invocation.task_id,
                    "user_id": invocation.user_id,
                    "step_id": invocation.step_id,
                    "call_id": invocation.call_id,
                    "status": invocation.status.value,
                    "snapshot": _native_snapshot(
                        encode_invocation(invocation), INVOCATION_JSON_PATHS
                    ),
                },
                session=self._session,
            )
        else:
            validate_transition(previous.status, invocation.status)
            require(
                replace(
                    previous,
                    status=invocation.status,
                    result=invocation.result,
                    error=invocation.error,
                )
                == invocation,
                "Invocation identity, arguments and approval binding are immutable",
            )
            result = await self._collection("invocations").replace_one(
                {
                    "invocation_id": invocation.invocation_id,
                    "user_id": self._user,
                    "status": previous.status.value,
                },
                {
                    "_id": invocation.invocation_id,
                    "invocation_id": invocation.invocation_id,
                    "run_id": invocation.run_id,
                    "task_id": invocation.task_id,
                    "user_id": invocation.user_id,
                    "step_id": invocation.step_id,
                    "call_id": invocation.call_id,
                    "status": invocation.status.value,
                    "snapshot": _native_snapshot(
                        encode_invocation(invocation), INVOCATION_JSON_PATHS
                    ),
                },
                session=self._session,
            )
            if result.matched_count != 1:
                raise conflict()
        await self._event(
            invocation.run_id,
            f"invocation.{invocation.status.value}",
            task_id=invocation.task_id,
            status=invocation.status,
        )

    async def get_approval(self, invocation_id: str) -> Approval | None:
        row = await self._collection("approvals").find_one(
            {"invocation_id": invocation_id, "user_id": self._user},
            session=self._session,
        )
        if row is None:
            return None
        return Approval(
            str(row["invocation_id"]),
            str(row["user_id"]),
            str(row["tool_version"]),
            str(row["argument_hash"]),
            bool(row["approved"]),
            datetime.fromisoformat(str(row["expires_at"])),
        )

    @mutation
    async def save_approval(self, approval: Approval) -> None:
        self._scope(approval.user_id)
        invocation = await self.get_invocation(approval.invocation_id)
        require(invocation is not None, "Invocation unavailable")
        assert invocation is not None
        require(
            invocation.status == InvocationStatus.AWAITING_APPROVAL
            and invocation.argument_hash == approval.argument_hash
            and invocation.tool_version == approval.tool_version
            and invocation.approval_expires_at == approval.expires_at
            and approval.expires_at > self._clock.utc_now(),
            "Approval binding is invalid or expired",
        )
        await self._collection("approvals").insert_one(
            {
                "_id": approval.invocation_id,
                "invocation_id": approval.invocation_id,
                "user_id": approval.user_id,
                "tool_version": approval.tool_version,
                "argument_hash": approval.argument_hash,
                "approved": approval.approved,
                "expires_at": approval.expires_at.isoformat(),
            },
            session=self._session,
        )
        await self.save_invocation(
            replace(
                invocation,
                status=InvocationStatus.READY if approval.approved else InvocationStatus.FAILED,
                error=None
                if approval.approved
                else JsonObject('{"code":"authorization","message":"Action denied"}'),
            ),
            InvocationStatus.AWAITING_APPROVAL,
        )

    async def artifacts(self, run_id: str) -> tuple[ArtifactRecord, ...]:
        return tuple(
            decode_artifact(
                row["snapshot"] if isinstance(row["snapshot"], str) else json.dumps(row["snapshot"])
            )
            for row in await self._find(
                "artifacts",
                {"run_id": run_id, "user_id": self._user},
                sort=(("name", ASCENDING), ("version", ASCENDING)),
            )
        )

    @mutation
    async def save_artifact(self, artifact: ArtifactRecord, expected: str | None) -> None:
        self._scope(artifact.user_id)
        row = await self._collection("artifacts").find_one(
            {"artifact_id": artifact.artifact_id, "run_id": artifact.run_id, "user_id": self._user},
            session=self._session,
        )
        previous = (
            decode_artifact(
                row["snapshot"] if isinstance(row["snapshot"], str) else json.dumps(row["snapshot"])
            )
            if row
            else None
        )
        if (previous.status if previous else None) != expected:
            raise conflict()
        document = {
            "_id": artifact.artifact_id,
            "artifact_id": artifact.artifact_id,
            "run_id": artifact.run_id,
            "task_id": artifact.task_id,
            "user_id": artifact.user_id,
            "name": artifact.name,
            "version": artifact.version,
            "status": artifact.status,
            "snapshot": json.loads(encode_artifact(artifact)),
        }
        if previous is None:
            require(artifact.status == "staged", "New manifest must be staged")
            await self._collection("artifacts").insert_one(document, session=self._session)
        else:
            require(
                previous.status == "staged"
                and artifact.status == "committed"
                and replace(previous, status="committed") == artifact,
                "Invalid manifest transition",
            )
            result = await self._collection("artifacts").replace_one(
                {"artifact_id": artifact.artifact_id, "status": "staged", "user_id": self._user},
                document,
                session=self._session,
            )
            if result.matched_count != 1:
                raise conflict()
        await self._event(artifact.run_id, f"artifact.{artifact.status}", task_id=artifact.task_id)

    @mutation
    async def create_step(self, step: Step) -> None:
        self._scope(step.user_id)
        require(step.status == StepStatus.REQUESTED, "New step must be requested")
        task = await self.get_task(step.task_id)
        require(
            task is not None and task.run_id == step.run_id and task.status == TaskStatus.RUNNING,
            "Step requires a running scoped task",
        )
        previous = await self.steps(step.run_id, step.task_id)
        require(step.ordinal == len(previous) + 1, "Step ordinals must be contiguous")
        require(
            all(item.status in {StepStatus.APPLIED, StepStatus.FAILED} for item in previous),
            "Prior step is unresolved",
        )
        await self._collection("steps").insert_one(
            {
                "_id": step.step_id,
                "step_id": step.step_id,
                "task_id": step.task_id,
                "run_id": step.run_id,
                "user_id": step.user_id,
                "ordinal": step.ordinal,
                "status": step.status.value,
                "snapshot": _native_snapshot(encode_step(step), STEP_JSON_PATHS),
            },
            session=self._session,
        )
        await self._event(
            step.run_id,
            "step.requested",
            task_id=step.task_id,
            version=step.ordinal,
            status=step.status,
        )

    async def steps(self, run_id: str, task_id: str | None = None) -> tuple[Step, ...]:
        criteria: Document = {"run_id": run_id, "user_id": self._user}
        if task_id is not None:
            criteria["task_id"] = task_id
        return tuple(
            decode_step(_snapshot_text(row["snapshot"], STEP_JSON_PATHS))
            for row in await self._find("steps", criteria, sort=(("ordinal", ASCENDING),))
        )

    @mutation
    async def update_step(
        self,
        step_id: str,
        expected: StepStatus,
        status: StepStatus,
        *,
        response: ModelResponse | None = None,
        error: JsonObject | None = None,
    ) -> Step:
        row = await self._collection("steps").find_one(
            {"step_id": step_id, "user_id": self._user},
            session=self._session,
        )
        require(row is not None, "Step is unavailable")
        assert row is not None
        step = decode_step(_snapshot_text(row["snapshot"], STEP_JSON_PATHS))
        if step.status != expected:
            raise conflict()
        allowed = {
            StepStatus.REQUESTED: {StepStatus.ACCEPTED, StepStatus.FAILED},
            StepStatus.ACCEPTED: {StepStatus.APPLIED},
            StepStatus.APPLIED: set(),
            StepStatus.FAILED: set(),
        }
        require(status in allowed[expected], "Invalid step transition")
        require(
            response is None or expected == StepStatus.REQUESTED,
            "Accepted decisions are immutable",
        )
        updated = replace(step, status=status, response=response or step.response, error=error)
        result = await self._collection("steps").replace_one(
            {"step_id": step_id, "user_id": self._user, "status": expected.value},
            {
                "_id": step_id,
                "step_id": step_id,
                "task_id": step.task_id,
                "run_id": step.run_id,
                "user_id": self._user,
                "ordinal": step.ordinal,
                "status": status.value,
                "snapshot": _native_snapshot(encode_step(updated), STEP_JSON_PATHS),
            },
            session=self._session,
        )
        if result.matched_count != 1:
            raise conflict()
        await self._event(
            step.run_id,
            f"step.{status.value}",
            task_id=step.task_id,
            version=step.ordinal,
            status=status,
        )
        return updated

    async def _event(
        self,
        run_id: str,
        event_type: str,
        *,
        task_id: str | None = None,
        version: int = 0,
        status: str | None = None,
        intent: ControlIntent | None = None,
        details: JsonObject | None = None,
    ) -> None:
        self.check_active()
        previous = await self._collection("events").find_one(
            {"run_id": run_id, "user_id": self._user},
            sort=[("sequence", DESCENDING)],
            session=self._session,
        )
        sequence = int(previous["sequence"]) + 1 if previous else 1
        event = Event(
            self._ids.new_id(),
            run_id,
            self._user,
            sequence,
            event_type,
            self._clock.utc_now(),
            JsonObject(
                json.dumps(
                    {
                        "version": version,
                        "status": status,
                        "intent": intent,
                        **(json.loads(details.text) if details is not None else {}),
                    }
                )
            ),
            task_id,
        )
        await self._collection("events").insert_one(
            {
                "_id": event.event_id,
                "event_id": event.event_id,
                "run_id": run_id,
                "user_id": self._user,
                "sequence": sequence,
                "task_id": task_id,
                "schema_version": event.schema_version,
                "event_type": event_type,
                "timestamp": event.timestamp.isoformat(),
                "payload": json.loads(event.payload.text),
            },
            session=self._session,
        )

    @mutation
    async def create_run(self, run: Run) -> None:
        self._scope(run.user_id)
        require(
            run.version == 0
            and run.status == RunStatus.CREATED
            and run.intent == ControlIntent.NONE
            and run.result is None
            and run.error is None,
            "New runs must be pristine",
        )
        await self._collection("runs").insert_one(
            {
                "_id": run.run_id,
                "run_id": run.run_id,
                "user_id": run.user_id,
                "status": run.status.value,
                "version": run.version,
                "snapshot": _native_snapshot(encode(run), RUN_JSON_PATHS),
                "control_intent": run.intent.value,
            },
            session=self._session,
        )
        await self._event(run.run_id, "run.created", status=run.status.value)

    async def get_run(self, run_id: str) -> Run | None:
        row = await self._collection("runs").find_one(
            {"run_id": run_id, "user_id": self._user},
            session=self._session,
        )
        return decode_run(_snapshot_text(row["snapshot"], RUN_JSON_PATHS)) if row else None

    async def _run(self, run_id: str, version: int) -> Run:
        run = await self.get_run(run_id)
        if run is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Run is unavailable to this principal")
        if run.version != version:
            raise conflict()
        return run

    async def _save_run(self, run: Run, previous: int, event_type: str) -> Run:
        result = await self._collection("runs").replace_one(
            {"run_id": run.run_id, "user_id": self._user, "version": previous},
            {
                "_id": run.run_id,
                "run_id": run.run_id,
                "user_id": self._user,
                "status": run.status.value,
                "version": run.version,
                "snapshot": _native_snapshot(encode(run), RUN_JSON_PATHS),
                "control_intent": run.intent.value,
            },
            session=self._session,
        )
        if result.matched_count != 1:
            raise conflict()
        await self._event(
            run.run_id,
            event_type,
            version=run.version,
            status=run.status.value,
            intent=run.intent,
        )
        return run

    @mutation
    async def transition_run(
        self,
        run_id: str,
        expected_version: int,
        status: RunStatus,
        *,
        result: JsonObject | None = None,
        error: JsonObject | None = None,
    ) -> Run:
        run = await self._run(run_id, expected_version)
        validate_transition(run.status, status)
        updated = replace(
            run,
            status=status,
            result=result,
            error=error,
            version=run.version + 1,
            updated_at=self._clock.utc_now(),
        )
        return await self._save_run(updated, expected_version, "run.transitioned")

    @mutation
    async def set_intent(self, run_id: str, expected_version: int, intent: ControlIntent) -> Run:
        run = await self._run(run_id, expected_version)
        require(bool(RUN_TRANSITIONS[run.status]), "Terminal runs are immutable")
        require(run.intent != ControlIntent.CANCEL, "Cancellation intent cannot be revoked")
        require(run.intent != intent, "Intent is already recorded")
        updated = replace(
            run,
            intent=intent,
            version=run.version + 1,
            updated_at=self._clock.utc_now(),
        )
        return await self._save_run(updated, expected_version, "run.intent_changed")

    @mutation
    async def create_task(self, task: Task) -> None:
        self._scope(task.user_id)
        run = await self.get_run(task.run_id)
        require(
            run is not None and bool(RUN_TRANSITIONS[run.status]),
            "Run is unavailable or terminal",
        )
        require(
            task.version == 0
            and task.status == TaskStatus.PENDING
            and task.result is None
            and task.error is None,
            "New tasks must be pristine",
        )
        for related_id in (*task.dependencies, task.parent_task_id):
            if related_id is None:
                continue
            related = await self.get_task(related_id)
            require(
                related is not None and related.run_id == task.run_id,
                "Related task must exist in the same run",
            )
        await self._collection("tasks").insert_one(
            {
                "_id": task.task_id,
                "task_id": task.task_id,
                "run_id": task.run_id,
                "user_id": task.user_id,
                "parent_task_id": task.parent_task_id,
                "status": task.status.value,
                "version": task.version,
                "snapshot": _native_snapshot(encode(task), TASK_JSON_PATHS),
            },
            session=self._session,
        )
        for dependency_id in task.dependencies:
            key = f"{task.task_id}:{dependency_id}"
            await self._collection("task_dependencies").insert_one(
                {
                    "_id": key,
                    "task_id": task.task_id,
                    "dependency_id": dependency_id,
                    "run_id": task.run_id,
                    "user_id": task.user_id,
                },
                session=self._session,
            )
        await self._ensure_budget_account(task)
        await self._event(
            task.run_id,
            "task.created",
            task_id=task.task_id,
            status=task.status.value,
        )

    async def get_task(self, task_id: str) -> Task | None:
        row = await self._collection("tasks").find_one(
            {"task_id": task_id, "user_id": self._user},
            session=self._session,
        )
        return decode_task(_snapshot_text(row["snapshot"], TASK_JSON_PATHS)) if row else None

    async def tasks(self, run_id: str) -> tuple[Task, ...]:
        return tuple(
            decode_task(_snapshot_text(row["snapshot"], TASK_JSON_PATHS))
            for row in await self._find(
                "tasks",
                {"run_id": run_id, "user_id": self._user},
                sort=(("task_id", ASCENDING),),
            )
        )

    async def delegated_task(self, parent_task_id: str, idempotency_key: str) -> Task | None:
        row = await self._collection("delegations").find_one(
            {
                "parent_task_id": parent_task_id,
                "idempotency_key": idempotency_key,
                "user_id": self._user,
            },
            session=self._session,
        )
        return await self.get_task(str(row["child_task_id"])) if row else None

    @mutation
    async def save_delegation(self, parent_task_id: str, idempotency_key: str, task: Task) -> None:
        self._scope(task.user_id)
        parent = await self.get_task(parent_task_id)
        require(
            parent is not None
            and task.parent_task_id == parent_task_id
            and task.run_id == parent.run_id,
            "Delegation parent and child must belong to the same run",
        )
        require(bool(idempotency_key.strip()), "Delegation idempotency key is required")
        await self._collection("delegations").insert_one(
            {
                "_id": f"{parent_task_id}:{idempotency_key}",
                "parent_task_id": parent_task_id,
                "idempotency_key": idempotency_key,
                "child_task_id": task.task_id,
                "run_id": task.run_id,
                "user_id": task.user_id,
            },
            session=self._session,
        )

    @mutation
    async def transition_task(
        self,
        task_id: str,
        expected_version: int,
        status: TaskStatus,
        *,
        result: JsonObject | None = None,
        error: JsonObject | None = None,
    ) -> Task:
        task = await self.get_task(task_id)
        if task is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Task is unavailable to this principal")
        if task.version != expected_version:
            raise conflict()
        validate_transition(task.status, status)
        updated = replace(
            task,
            status=status,
            result=result,
            error=error,
            version=task.version + 1,
            updated_at=self._clock.utc_now(),
        )
        result_doc = await self._collection("tasks").replace_one(
            {
                "task_id": task_id,
                "user_id": self._user,
                "version": expected_version,
            },
            {
                "_id": task_id,
                "task_id": task_id,
                "run_id": task.run_id,
                "user_id": self._user,
                "parent_task_id": task.parent_task_id,
                "status": status.value,
                "version": updated.version,
                "snapshot": _native_snapshot(encode(updated), TASK_JSON_PATHS),
            },
            session=self._session,
        )
        if result_doc.matched_count != 1:
            raise conflict()
        await self._event(
            task.run_id,
            "task.transitioned",
            task_id=task_id,
            version=updated.version,
            status=status.value,
        )
        return updated

    @mutation
    async def set_task_handoff(
        self, task_id: str, expected_version: int, handoff: JsonObject
    ) -> Task:
        task = await self.get_task(task_id)
        if task is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Task is unavailable to this principal")
        if task.version != expected_version:
            raise conflict()
        require(task.status == TaskStatus.READY, "Handoff can only be finalized before execution")
        updated = replace(
            task,
            handoff=handoff,
            version=task.version + 1,
            updated_at=self._clock.utc_now(),
        )
        result = await self._collection("tasks").replace_one(
            {
                "task_id": task_id,
                "user_id": self._user,
                "version": expected_version,
            },
            {
                "_id": task_id,
                "task_id": task_id,
                "run_id": task.run_id,
                "user_id": self._user,
                "parent_task_id": task.parent_task_id,
                "status": task.status.value,
                "version": updated.version,
                "snapshot": _native_snapshot(encode(updated), TASK_JSON_PATHS),
            },
            session=self._session,
        )
        if result.matched_count != 1:
            raise conflict()
        await self._event(
            task.run_id,
            "task.handoff_finalized",
            task_id=task_id,
            version=updated.version,
        )
        return updated

    async def events(self, run_id: str, *, after: int = 0, limit: int = 100) -> tuple[Event, ...]:
        self.check_active()
        require(after >= 0 and 1 <= limit <= 1000, "Invalid event pagination")
        rows = await self._find(
            "events",
            {"run_id": run_id, "user_id": self._user, "sequence": {"$gt": after}},
            sort=(("sequence", ASCENDING),),
            limit=limit,
        )
        return tuple(
            Event(
                str(row["event_id"]),
                str(row["run_id"]),
                str(row["user_id"]),
                int(row["sequence"]),
                str(row["event_type"]),
                datetime.fromisoformat(str(row["timestamp"])),
                JsonObject(_stored_json_text(row["payload"])),
                row.get("task_id"),
                int(row["schema_version"]),
            )
            for row in rows
        )

    async def load_checkpoint(self, task_id: str) -> Checkpoint | None:
        row = await self._collection("checkpoints").find_one(
            {"task_id": task_id, "user_id": self._user},
            session=self._session,
        )
        if row is None:
            return None
        return Checkpoint(
            str(row["task_id"]),
            str(row["run_id"]),
            str(row["user_id"]),
            int(row["state_version"]),
            int(row["last_event_sequence"]),
            JsonObject(_stored_json_text(row["continuation"])),
            int(row["version"]),
            int(row["schema_version"]),
        )

    @mutation
    async def save_checkpoint(self, checkpoint: Checkpoint, expected_version: int | None) -> None:
        self._scope(checkpoint.user_id)
        task = await self.get_task(checkpoint.task_id)
        require(
            task is not None and task.run_id == checkpoint.run_id,
            "Checkpoint task is unavailable",
        )
        assert task is not None
        if task.version != checkpoint.state_version:
            raise conflict()
        previous = await self.load_checkpoint(checkpoint.task_id)
        if (previous.version if previous else None) != expected_version:
            raise conflict()
        require(
            checkpoint.version == (0 if previous is None else previous.version + 1),
            "Checkpoint version must advance exactly once",
        )
        if previous is not None:
            require(
                checkpoint.last_event_sequence >= previous.last_event_sequence,
                "Checkpoint event cursor cannot move backwards",
            )
        task_events = await self._find(
            "events",
            {"run_id": task.run_id, "task_id": task.task_id, "user_id": self._user},
            sort=(("sequence", DESCENDING),),
        )
        cursor_event = await self._collection("events").find_one(
            {
                "run_id": task.run_id,
                "sequence": checkpoint.last_event_sequence,
                "user_id": self._user,
            },
            session=self._session,
        )
        require(
            bool(task_events)
            and cursor_event is not None
            and checkpoint.last_event_sequence >= int(task_events[0]["sequence"]),
            "Checkpoint cursor is missing or misses task events",
        )
        await self._collection("checkpoints").replace_one(
            {"task_id": checkpoint.task_id, "user_id": self._user},
            {
                "_id": checkpoint.task_id,
                "task_id": checkpoint.task_id,
                "run_id": checkpoint.run_id,
                "user_id": checkpoint.user_id,
                "schema_version": checkpoint.schema_version,
                "version": checkpoint.version,
                "state_version": checkpoint.state_version,
                "last_event_sequence": checkpoint.last_event_sequence,
                "continuation": json.loads(checkpoint.continuation.text),
            },
            upsert=expected_version is None,
            session=self._session,
        )
