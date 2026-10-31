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
async with MongoStore(uri, "test", clock, ids) as store:
    async with store.transaction("local-user") as tx:
        await tx.create_run(run)
        await tx.create_task(task)
```

`StateStore` and `StateTransaction` are Protocols. `MongoStore` implements the
scoped repository operations in MongoDB sessions and transactions. Creation and transition
methods append events automatically; callers cannot forget the corresponding
event. Successful context exit commits; errors/cancellation roll back. A caught
mutation failure still prevents commit. Repository handles expire at context
exit and cannot be used by another coroutine. Nested transactions fail promptly.

A process-local asyncio lock serializes short transaction units so per-run event
sequences remain ordered. MongoDB unique indexes and expected-version filters
enforce key and optimistic-concurrency invariants; repository methods validate
relationship scope. Atlas replica-set support is required for transactions. Do
not await model/tool work, resource admission or approval inside a transaction.
Transactions must not be nested or shared with another coroutine. The harness
still assumes one owning service process; cross-process scheduling ownership is
future recovery work.

`get_run`, `get_task`, `load_checkpoint` return None for missing or unowned IDs.
Event queries return only the principal's records, ordered by per-run sequence,
with exclusive `after` pagination and a 1..1000 page size. Mutation methods reject
unowned state. Status updates require the current expected version and increment
it exactly once. Conflicts use the machine-readable concurrency_conflict code.

Control intent is durable independently of status: recording cancellation does
not claim that work has stopped. Cancellation intent cannot be revoked. This
phase records intent only; scheduling and cancellation effects come later.

## MongoDB collections

| Collection | Key / indexes | Role |
| --- | --- | --- |
| runs | run UUID; user/status index | Run snapshot, status/version and control intent |
| tasks | task UUID; run/user/status index | Task snapshot, parent and assigned agent |
| task_dependencies | task/dependency/run/user unique index | Durable dependency references |
| delegations | parent/idempotency-key unique; child unique | Idempotent child creation |
| events | run/sequence unique index | Append-only versioned metadata |
| checkpoints | task UUID | Versioned serialized continuation |
| steps | step UUID; task/ordinal unique index | Complete model request and accepted response |
| invocations | invocation UUID; step/call unique index | Tool identity, arguments, state, result and error |
| approvals | invocation UUID | User decision bound to exact tool version and arguments |
| artifacts | artifact UUID; user/run/task/name/version unique index | Bounded artifact manifest and integrity metadata |
| budget_accounts | task UUID; run/user index | Hierarchical limits, used/reserved totals, unknown usage count |
| budget_reservations | reservation UUID; run/task/status index | Idempotent per-operation reservation and reconciliation state |

Collections and indexes are ensured at application startup; MongoDB has no
SQLite-style numbered schema migrations here. Existing local SQLite databases
are intentionally not imported. The configured URI is held in `SecretStr`, is
excluded from run snapshots, and should be provided through
`AGENT_HARNESS_MONGODB_URI`.
Structured snapshots, event payloads, and checkpoint continuations are stored
as native BSON documents/arrays rather than JSON text. Readers still accept
the earlier string-encoded format for existing records.

## Hierarchical budgets and completion (Phase 6)

`budget_accounts` and `budget_reservations` are MongoDB collections and are
updated in the same MongoDB transaction as the corresponding run events.
Reservations charge the task and all ancestors. Duplicate reservation IDs with
the same task and amount are idempotent; reuse with different work is rejected.
Reconciliation is immutable and idempotent. Unknown usage consumes the
reserved amount and increments `unknown_usage_count`. Model attempts reserve one
step and a conservative token bound before generation; known provider usage
replaces that token bound on reconciliation. Tool attempts reserve one call
before execution. Retries create separate attempts and therefore consume
additional budget.

Completion decisions are deterministic and appended to the existing MongoDB
`events` collection as `completion.validated`. A candidate must have no
unresolved tool actions or required children; configured committed artifact
names and JSON Schema constraints are also checked. Failed candidates receive
bounded correction attempts. Partial child outcomes require the explicit
`partial_result_policy = "allow"` setting.

The legacy `SQLiteStore` remains for existing local development compatibility;
its Phase 6 budget counters are process-local only and it does not persist
budget or completion records. Production composition uses MongoDB. MongoDB
transaction behavior has not been live-validated against Atlas for this phase.

## Database and agent trace

The default MongoDB database name is `test`. There is no second trace database
to keep synchronized. The same MongoDB database contains the full durable trace:
`steps` stores the exact model and messages
sent plus the accepted assistant response; `invocations` stores tool arguments,
status, result and error; `events` stores the ordered run/task/step/tool state
transitions. The `trace RUN_ID` CLI command combines these records, including
private prompt and tool data, after enforcing run ownership. `runs` and `tasks`
hold current state, `checkpoints` enables continuation, `approvals` records
write decisions, and `artifacts` indexes workspace outputs. `task_dependencies`
records child execution dependencies, and `delegations` maps parent/idempotency
key pairs to child tasks to prevent duplicate creation after repeated requests.

This trace contains potentially sensitive prompts and tool data. Restrict
database credentials and access, and avoid sharing trace output without reviewing it.

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

Invocation transition validation does not by itself prove task acceptance
criteria or authorize side effects; those checks belong to application services.

## Limitations

The API enforces principal scope but is not an authentication service. No process
ownership lock, crash recovery scheduler, or startup reconciliation exists yet;
those belong to Phase 7. Atlas transaction behavior and cross-process budget
admission remain unverified.
