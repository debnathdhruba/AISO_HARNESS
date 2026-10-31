# Phase 6 review handoff

## Scope delivered

- Added typed limits, usage, and budget snapshots.
- Added MongoDB `budget_accounts` and `budget_reservations` collections,
  ancestor-wide reservations, idempotent reconciliation, and explicit unknown
  usage charging. Budget lifecycle events use the existing MongoDB `events`
  collection.
- Model attempts reserve step/token capacity before generation; tool attempts
  reserve tool-call capacity before execution. Configured transient-provider
  retries consume a new reservation per attempt.
- Added deterministic artifact, required-phrase, and JSON Schema completion
  checks, with bounded correction attempts.
- Added a fail-fast/allow-partial child policy. Partial output is accepted only
  under the explicit `allow` setting and when successful child work exists.
- Added configuration and user/developer documentation. No test files were
  created.
- The legacy SQLite adapter keeps only in-memory budget counters for existing
  development compatibility; no Phase 6 budget or completion data is persisted
  to SQLite. Runtime persistence remains MongoDB-only.

## Validation

| Check | Result |
| --- | --- |
| `python -m mypy src` | Passed: no issues in 48 source files |
| Ruff on changed Phase 6 source files | Passed |
| `python -m compileall -q src\agent_harness` | Passed |
| Inline config, budget-value, and completion-validation smoke checks | Passed |
| Existing `python -m pytest -q` | Failed: 14 failed, 80 passed |
| Live MongoDB/Atlas connection | Not run |

The existing suite failures include stale dependency/migration/CLI expectations,
tool registration and authorization failures, and a workspace artifact-search
failure. A representative tool run reports `authorization: Tool/version is not
registered`. These failures were not addressed as Phase 6 work. The suite does
not verify the MongoDB budget implementation.

## Persistence model

`budget_accounts` stores the configured task/run limits, used totals, active
reservations, and unknown-usage count. `budget_reservations` records the
reservation ID, charged ancestors, reserved amount, reconciliation state, and
actual usage. Both are updated inside MongoDB transactions. `completion.validated`
records are appended to the existing `events` collection.

## Limitations and review points

- Atlas transactions and concurrent/cross-process admission have not been
  exercised against a live database. The store currently serializes
  transaction units within one process; stronger process ownership/recovery is
  Phase 7.
- Provider-reported token counts are charged only when both input and output
  counts are present. Otherwise, the full reserved amount is charged.
- Actual provider usage can exceed the requested reservation; reconciliation
  records actual usage, so an account may exceed its configured limit after the
  external operation has already completed.
- The configured result schema validates the final candidate object, which has
  the shape `{"answer": "..."}`.

Phase 7 has not been started. Await human review before proceeding.
