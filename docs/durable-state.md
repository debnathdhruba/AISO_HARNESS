# Phase 1 durable state contracts

Implemented sources: `domain/entities.py`, `domain/states.py`, `domain/errors.py`,
`protocols/state.py`, `adapters/system.py`, and `memory/backends/` (the original
Phase 1 state backend, now grouped under the unified memory package).

## Entities and identifiers

RunConfig snapshots definition/version, model, policy, budget, and memory
settings. Run stores principal, input, status, timestamps, deadline, version,
control intent, result and error. Task stores scope, objective, acceptance criteria,
parent/dependencies, assigned instance ID, status, timestamps, deadline, version
and result. IDs are canonical UUIDs; UUID generation and UTC/monotonic time are
injected. Principals are explicit strings, not agent names.

Snapshots are frozen dataclasses with validated canonical JSON objects for
structured payloads. Never put futures, connections, credentials or callables in
continuations. Event schema version 1 and checkpoint schema versions 1 and 2 are
supported; unknown versions fail rather than being silently accepted. Typed
provider/model contracts, attempt records and agent definitions will be
introduced as consumed.

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
uses a MongoDB lease to enforce one owning scheduler process per database.

`get_run`, `get_task`, `load_checkpoint` return None for missing or unowned IDs.
Event queries return only the principal's records, ordered by per-run sequence,
with exclusive `after` pagination and a 1..1000 page size. Mutation methods reject
unowned state. Status updates require the current expected version and increment
it exactly once. Conflicts use the machine-readable concurrency_conflict code.

Control intent is durable independently of status: recording pause or
cancellation does not claim that work has stopped. Control requests take effect
at safe execution boundaries; cancellation intent cannot be revoked.

## MongoDB collections

| Collection | Key / indexes | Role |
| --- | --- | --- |
| telemetry | `_kind` plus record-specific fields and IDs | All durable run traces: runs, tasks, dependencies, delegations, events, checkpoints, model steps, tool invocations, and approvals |
| memory | `_kind=memory_records`; user/deletion/sharing/owner/update index | User-scoped private/shared facts, retrieval tags, provenance and soft deletion |
| control | `_kind` plus budget/lease IDs and query fields | Budget accounts/reservations and scheduler ownership lease |

Collections and indexes are ensured at application startup. New MongoDB state
uses these three collections; existing per-entity collections are left as-is and
are not migrated or deleted. Existing local SQLite databases are intentionally
not imported. The configured URI is held in `SecretStr`, is
excluded from run snapshots, and should be provided through
`AGENT_HARNESS_MONGODB_URI`.
Structured snapshots, event payloads, and checkpoint continuations are stored
as native BSON documents/arrays rather than JSON text. Readers still accept
the earlier string-encoded format for existing records.

## Hierarchical budgets and completion (Phase 6)

Budget account and reservation documents in `control` are updated in the same
MongoDB transaction as the corresponding run events in `telemetry`.
Reservations charge the task and all ancestors. Duplicate reservation IDs with
the same task and amount are idempotent; reuse with different work is rejected.
Reconciliation is immutable and idempotent. Unknown usage consumes the
reserved amount and increments `unknown_usage_count`. Model attempts reserve one
step and a conservative token bound before generation; known provider usage
replaces that token bound on reconciliation. Tool attempts reserve one call
before execution. Retries create separate attempts and therefore consume
additional budget.

Completion decisions are deterministic and appended to the `events` record kind
in MongoDB `telemetry` as `completion.validated`. A candidate must have no
unresolved tool actions or required children; configured JSON Schema constraints
are also checked. Failed candidates receive
bounded correction attempts. Partial child outcomes require the explicit
`partial_result_policy = "allow"` setting.

The legacy `SQLiteStore` remains for existing local development compatibility;
its Phase 6 budget counters are process-local only and it does not persist
budget or completion records. Production composition uses MongoDB. MongoDB
transaction behavior has not been live-validated against Atlas for this phase.

## Recovery and process ownership (Phase 7)

The fixed `process_ownership` document in `control` is a MongoDB lease with owner UUID,
process ID, heartbeat, and expiry. A scheduler process must acquire the lease
before startup recovery or task execution. A heartbeat renews it; loss of the
lease stops new model/tool admissions and prevents the stale process from
committing model decisions. Scheduler-owned transactions conditionally renew
the lease inside the Mongo transaction as a fencing check. Read-only inspection
and persisted control requests do not acquire the scheduling lease.

Startup recovery runs under the lease. It validates task parent/dependency
references and cycles, conservatively reconciles stale budget reservations,
marks interrupted model requests as failed and eligible for one recovery retry,
requeues interrupted read-only/idempotent invocations, and marks interrupted
non-idempotent writes `unknown`. It pauses still-live runs with schema-version-2
checkpoints and fails expired runs rather than extending their original
deadlines. Delegation records and accepted model decisions remain immutable,
so replay uses their existing idempotency keys and does not create duplicate
children or tool intents.

Unknown invocation outcomes can be resolved through the explicit reconciliation
port/CLI only while the run is paused. The caller must independently verify the
side effect and provide exactly one observed result or failure; resuming is
blocked while unknown outcomes remain. `recover RUN_ID --user USER` performs
startup recovery and displays the resulting state without resuming that run.
`resume` is explicit and retains the original run deadline and configuration.
Control intent is durable: `pause` takes effect at a safe boundary, while
`cancel` is finalized at a safe boundary or during startup recovery.

Checkpoint schema version 1 remains readable. New checkpoints use schema
version 2 and preserve the recovery decision in their continuation. Event
schema remains version 1. This phase has not been tested against a live MongoDB
deployment; lease behavior, transaction fencing, and restart semantics still
need deployment-level validation.

## Database and agent trace

The default MongoDB database name is `test`. There is no second trace database
to keep synchronized. The same MongoDB database contains the full durable trace
in `telemetry`: typed `steps` records store the exact model messages and accepted
response; `invocations` records store tool arguments, status, result and error;
`events` records store ordered state transitions. The `trace RUN_ID` CLI command
combines these records, including private prompt and tool data, after enforcing
run ownership. Typed `runs` and `tasks` records hold current state, checkpoints
enable continuation, and approvals record write decisions. Typed dependency
and delegation records preserve child
execution requirements and prevent duplicate child creation.

This trace contains potentially sensitive prompts and tool data. Restrict
database credentials and access, and avoid sharing trace output without reviewing it.

## Resource admission (Phase 4)

The application composition root owns one `ResourceScheduler` and shares it with
all run runners in that application lifetime. Model calls use the configured
provider key; tools and memory operations use separate bounded
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
| Task | paused | pending, ready, failed, cancelled |
| Task | succeeded / failed / cancelled / blocked | none |
| Invocation | planned | awaiting_approval, ready, failed, cancelled |
| Invocation | awaiting_approval | ready, failed, cancelled |
| Invocation | ready | executing, failed, cancelled |
| Invocation | executing | ready (safe recovery replay), succeeded, failed, cancelled, unknown |
| Invocation | unknown | succeeded, failed, via explicit reconciliation only |
| Invocation | succeeded / failed / cancelled | none |

Invocation transition validation does not by itself prove task acceptance
criteria or authorize side effects; those checks belong to application services.

## Limitations

The API enforces principal scope but is not an authentication service. MongoDB
lease behavior, transaction fencing, and restart semantics have not been
validated against a live deployment. Atlas transaction behavior and
cross-process budget admission remain unverified.

## User memory and context (Phase 8)

`memory` is the MongoDB-only durable store for selected facts. Each
record has a user, owning agent definition, private/shared policy, tags,
optional run/task provenance, creation/update timestamps, and soft-deletion
timestamp. Private reads require the same user and owner definition; shared
reads may cross agent definitions but never users. Only the owning definition
may update or delete a record. Direct ID lookup uses the same filters as search,
so IDs do not bypass scope checks.

`MemoryService` uses a deterministic lexical retriever behind a provider
protocol. Retrieval is bounded to 200 scoped candidates, 20 results and a
configured rendered-context byte budget. The resource scheduler's memory lane
admits reads and writes. Embedding/semantic retrieval is an extension point;
this phase does not require a vector database.

Memory extraction defaults off. When enabled for a run, the `memory_save` tool
is exposed with instructions to save only when the user explicitly asks the
agent to remember something. The caller must choose `private` or `shared`;
shared facts are still scoped to the same user. The tool uses its durable
invocation ID as the memory record ID, so a replay after a crash does not
duplicate a saved fact. No automatic extraction of all conversation text,
tool output, credentials or checkpoints occurs.

When retrieval is enabled, matching records are injected once into each
constructed model request as provenance-bearing, JSON-encoded untrusted data,
separate from checkpoint continuation and tool history. The context builder
enforces the existing request byte limit and preserves whole tool-call/result
groups. Retrieval/storage errors fail the run explicitly rather than silently
omitting requested context.
