# Phase 1 durable state contracts

Implemented sources: `domain/entities.py`, `domain/states.py`, `domain/errors.py`,
`ports/state.py`, `adapters/system.py`, and `persistence/`.

## Entities and identifiers

RunConfig snapshots definition/version, model, policy, budget, memory and workspace
settings. Model settings retain separate workspace and artifact-root paths for
recovery. Run stores principal, input, status, timestamps, deadline, version,
control intent, result and error. Task stores scope, objective, acceptance criteria,
parent/dependencies, assigned instance ID, status, timestamps, deadline, version
and result. IDs are canonical UUIDs; UUID generation and UTC/monotonic time are
injected. Principals are explicit strings, not agent names.

Snapshots are frozen dataclasses with validated canonical JSON objects for
structured payloads. Never put futures, connections, credentials or callables in
continuations. Schema version 1 is supported for snapshots/events/checkpoints;
unknown versions fail rather than being silently accepted. Typed provider/model
contracts, attempt records and agent definitions will be introduced as consumed.

## Transactions and repositories

```python
async with SQLiteStore(path, clock, ids) as store:
    async with store.transaction("local-user") as tx:
        await tx.create_run(run)
        await tx.create_task(task)
```

`StateStore` and `StateTransaction` are Protocols. The SQLite adapter implements
the scoped repository operations within one unit of work. Creation and transition
methods append events automatically; callers cannot forget the corresponding
event. Successful context exit commits; errors/cancellation roll back. A caught
mutation failure still prevents commit. Repository handles expire at context
exit and cannot be used by another coroutine. Nested transactions fail promptly.

A store-wide asyncio lock serializes transactions before BEGIN IMMEDIATE. SQLite
foreign keys enforce related scope and WAL enables ordinary external readers.
Do not await external model/tool work, resource admission or approval inside a
transaction. A caller must not await a child coroutine that tries to open another
transaction while holding its own transaction. Transactions are intentionally
short and are not independent parallel operations on a shared connection.

`get_run`, `get_task`, `load_checkpoint` return None for missing or unowned IDs.
Event queries return only the principal's records, ordered by per-run sequence,
with exclusive `after` pagination and a 1..1000 page size. Mutation methods reject
unowned state. Status updates require the current expected version and increment
it exactly once. Conflicts use the machine-readable concurrency_conflict code.

Control intent is durable independently of status: recording cancellation does
not claim that work has stopped. Cancellation intent cannot be revoked. This
phase records intent only; scheduling and cancellation effects come later.

## Database schema and evolution

| Table | Key/ownership | Role |
| --- | --- | --- |
| schema_migrations | ordered contiguous version | Applied schema history |
| runs | UUID; unique run/user pair | Run snapshot, status/version, control intent |
| tasks | UUID; unique task/run/user triple | Task snapshot and parent FK |
| task_dependencies | task/dependency pair; scoped FKs | Durable dependency references |
| events | run/sequence; unique event UUID | Append-only versioned metadata |
| checkpoints | task UUID; scoped task FK and event FK | Versioned serialized continuation |
| steps | task/ordinal; run-scoped snapshot | Complete model request and accepted response for each reasoning step |
| invocations | step/call; run-scoped snapshot | Tool identity, arguments, state, result and error for each call |
| approvals | invocation UUID | User approval/denial bound to exact tool version and arguments |
| artifacts | artifact UUID and task/name/version | Bounded local artifact manifest and integrity metadata |

Migration 1 creates base state, relationships, events, checkpoints, indexes and
append-only event triggers. Migration 2 adds the run control_intent column with a
default of none. Later migrations add model steps, tool invocations, approvals
and artifact manifests. All pending migrations run in one transaction.
Unsupported or non-contiguous history is rejected. Opening a current database
is idempotent.

## Local database and agent trace

The default path is `.local/harness.sqlite`. TOML examples use the same path;
separate example-specific database files are not needed. The `database`
setting/CLI option remains available when a user explicitly wants isolated
state. SQLite's `-wal` and `-shm` files are temporary sidecars, not separate
application databases.

There is no second trace database to keep synchronized. The same SQLite file
contains the full durable trace: `steps` stores the exact model and messages
sent plus the accepted assistant response; `invocations` stores tool arguments,
status, result and error; `events` stores the ordered run/task/step/tool state
transitions. The `trace RUN_ID` CLI command combines these records, including
private prompt and tool data, after enforcing run ownership. `runs` and `tasks`
hold current state, `checkpoints` enables continuation, `approvals` records
write decisions, and `artifacts` indexes workspace outputs. `task_dependencies`
is ready for durable dependency relationships; it is currently empty when no
multi-task coordination has been implemented.

This trace is local but contains potentially sensitive prompts and tool data.
Protect the database file with host permissions and avoid sharing trace output
without reviewing it.

## Resource admission (Phase 4)

The application composition root owns one `ResourceScheduler` and shares it with
all run runners in that application lifetime. Model calls use the configured
provider key; tools and filesystem/artifact operations use separate bounded
lanes. A reserved memory lane is available for the later memory phase. Limits
include global active capacity, finite queue length, and model per-provider
capacity. Queue-full, queued cancellation, timeout, and shutdown are distinct
outcomes; work rejected before admission is never invoked. `snapshot()` reports
per-lane active, queued, configured capacity, and shutdown state.

The blocking adapter bounds submitted synchronous tools with fixed workers plus
a finite queue. Cancelling or timing out its asyncio caller does not terminate a
Python worker thread. The adapter retains the slot and drains in-flight work on
graceful shutdown; any write whose result was abandoned remains subject to the
existing uncertain-side-effect policy.

Checkpoint creation uses expected_version=None and version=0. Replacement requires
the previous checkpoint version and increments it by one. Its state_version must
match the current task. The event cursor must cover the task's latest event and
reference an existing event in the same run; it cannot move backward. Save state,
events and checkpoint together when advancing a durable continuation.

Dependencies and parents must already exist in the same run. Consequently the
Phase 1 immutable dependency graph cannot introduce cycles. Dynamic delegation,
graph editing and readiness evaluation are not implemented here.

## Transition semantics

The complete machine-readable tables are in `domain/states.py`; tests exercise
every source/target pair. No self-transitions or terminal reopenings are allowed.

| Machine | State | Allowed targets |
| --- | --- | --- |
| Run | created | queued, failed, cancelled |
| Run | queued | running, paused, failed, cancelled |
| Run | running | waiting, paused, completed, partial, failed, cancelled |
| Run | waiting | running, paused, failed, cancelled |
| Run | paused | queued, failed, cancelled |
| Run | completed / partial / failed / cancelled | none |
| Task | pending | ready, paused, blocked, failed, cancelled |
| Task | ready | running, paused, blocked, failed, cancelled |
| Task | running | waiting_children, waiting_approval, paused, succeeded, failed, cancelled |
| Task | waiting_children | ready, paused, blocked, failed, cancelled |
| Task | waiting_approval | ready, paused, failed, cancelled |
| Task | paused | pending, failed, cancelled |
| Task | succeeded / failed / cancelled / blocked | none |
| Invocation | planned | awaiting_approval, ready, failed, cancelled |
| Invocation | awaiting_approval | ready, failed, cancelled |
| Invocation | ready | executing, failed, cancelled |
| Invocation | executing | succeeded, failed, cancelled, unknown |
| Invocation | unknown | succeeded, failed, via future explicit reconciliation only |
| Invocation | succeeded / failed / cancelled | none |

Invocation states are contracts only; no invocation repository/executor exists.
Transition validation does not prove task acceptance criteria, authorize side
effects, enforce deadlines or implement budgets. Those belong to later services.

## Limitations

SQLite files hold private inputs/results/configuration in plain local storage;
event payloads exclude them. Protect the local file with host access controls.
The API enforces principal scope but is not an authentication service. No process
ownership lock, crash recovery scheduler, budget ledger, model call or tool effect
exists yet. Single-cancellation rollback is tested; hard process interruption and
repeated cancellation during cleanup belong to the later recovery phase.
