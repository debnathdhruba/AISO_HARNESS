# Initial architecture decisions

Status: accepted for the Phase 0 scaffold; subject to human architecture review.
Date: 2026-10-31. Each numbered decision remains independently revisable.

## ADR-001: Standalone boundary

Build an independent distribution with no reference-framework imports, SDKs,
servers, configuration or data dependencies. Use `AIOS_HARNESS` per the explicit
user request. Preserve existing files and do not modify the reference checkout.
Consequence: the harness owns its complete runtime rather than wrapping AIOS.

## ADR-002: Asyncio concurrency

Use Python 3.11+ asyncio and explicit structured task lifetimes in one process.
Separate task admission from resource admission. Blocking adapters later receive
bounded execution capacity; timeout cannot falsely imply thread termination.
Reject thread-per-request and speculative distributed workers.

## ADR-003: SQLite ownership

Use SQLite plus aiosqlite, migrations, foreign keys and WAL where supported.
Serialize transaction units explicitly; persist state/events together and use
optimistic versions. Add enforceable process ownership during recovery work.
SQLite is the authority; in-memory futures are only handles. No external database
is required, at the cost of a single-host scheduling boundary.

## ADR-004: Model and tool ports

Own normalized message, usage, request, response and invocation types. Use narrow
Protocols and injected adapters, with fake adapters first and contract tests.
Persist accepted model decisions and stable tool invocation identities. Validate
arguments and trusted policy before execution. Implement signatures in Phase 1
as needed, avoiding speculative empty interfaces in Phase 0.

## ADR-005: Original implementation

Write original code against the specification. Architectural responsibility names
are not permission to copy the reference implementation. Do not vendor, install,
invoke or import AIOS/Cerebrum. Review source and dependency metadata at each gate.

## ADR-006: Scope and phase gates

Initially exclude distributed execution, untrusted plugins, arbitrary generated
code, shell/browser/VM tools, marketplaces, mandatory embeddings and GPU models.
Implement only the authorized phase, then provide evidence and stop for review.
Only Phase 0 is authorized at this handoff. No placeholders imply working runtime
capabilities. Add dependencies and directories in the phase that needs them.

## ADR-007: Reproducible tooling

Use a src-layout package built with Hatchling, strict mypy, Ruff and pytest.
Resolve released tooling against Python 3.11 using uv; commit uv.lock rather than
guessing dependency versions. Include Hatchling in the development lock so build
tooling is recorded too. Runtime dependencies remain empty until used. The locked
workflow is canonical; unpinned pip editable installation remains supported.

## ADR-008: Phase 1 durable state boundary

Phase 1 is now authorized and implements RunConfig, Run, Task, Event and
Checkpoint. Other entities arrive when consumed by later phases. UUID strings
identify durable objects; user identity is an explicit nonempty principal string
so trusted-local identities such as `local-user` remain valid. Frozen dataclasses
and canonical immutable JSON snapshots prevent accidental in-place mutation.
Field-specific configuration validation comes with the corresponding services.

Use one connection per store and one coroutine owner per unit of work. Reads and
writes both use the same scoped transaction interface; no raw connection escapes.
Commit on successful context exit; rollback on exception or cancellation. Any
failed mutation poisons the transaction even if its exception is caught, avoiding
partial state without its event. No model/tool/queue waits belong inside a UoW.

Runs/tasks use optimistic versions; checkpoints have an independent revision and
reference a task state version plus a durable event cursor. Event payloads contain
only authored status, intent and version metadata. Database snapshots themselves
are private state, not redacted logs. Append-only events are enforced by triggers.

Two schema migrations establish the base tables and then add indexed-row control
intent storage; an existing version-1 fixture verifies upgrade and data retention.
This does not introduce scheduling or process leadership. Unknown invocation
outcomes permit only reconciliation to succeeded/failed, never automatic execution.
Blocked tasks are terminal; paused tasks return to pending for dependency evaluation.
Retries are not implemented and cannot reopen terminal records. A later retry
feature must add separate attempt records instead of resetting versions/status.

## ADR-009: MongoDB persistence backend

Status: accepted at the user's request; supersedes the SQLite runtime choice in
ADR-003. The harness now uses PyMongo's asynchronous client and MongoDB
transactions for run state, steps, invocations, events, checkpoints, approvals,
artifact manifests, task dependencies and idempotent delegations. The default
database is `test`; credentials are supplied through
`AGENT_HARNESS_MONGODB_URI`, represented as a secret setting, and excluded from
run snapshots.

Existing SQLite data is not migrated or read. The earlier SQLite adapter remains
only to support the unchanged legacy test suite; it is not wired into runtime
bootstrap or packaged as a runtime dependency. MongoDB Atlas replica-set
transactions are required. A process-local transaction lock preserves per-run
event ordering; multi-process ownership/recovery remains a later phase.
