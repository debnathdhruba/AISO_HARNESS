# Phase 7 review handoff

## Scope delivered

- Added the MongoDB `process_ownership` scheduler lease, heartbeat, transaction
  fencing, and ownership checks before model/tool work and decision commits.
- Added startup recovery for task-tree validation, stale budget reservations,
  interrupted model requests, replayable invocations, unknown write outcomes,
  paused checkpoints, and expired runs.
- Added persisted pause/cancel requests, guarded resume, explicit unknown-effect
  reconciliation, and CLI commands `pause`, `cancel`, `resume`, `recover`, and
  `reconcile`.
- Added checkpoint schema version 2 while retaining version 1 compatibility.
  Event schema remains version 1.
- Updated README, durable-state contract, and phase tracker. No test files were
  created or modified.

## Validation

| Check | Result |
| --- | --- |
| `python -m mypy src` | Passed: no issues in 48 source files |
| Changed Phase 7 source Ruff check | Passed |
| Changed Phase 7 source Ruff formatting | Passed; `mongo.py` was formatted |
| `python -m compileall -q src` | Passed |
| Focused existing recovery/domain tests | Passed: 26 passed, 1 deselected |
| Full existing `python -m pytest -q` | 79 passed, 15 failed |
| Checkpoint v1/v2 and recovery transition smoke check | Passed without creating files |
| CLI `recover --help` and `reconcile --help` | Passed |
| Live MongoDB/Atlas connection or lease test | Not run |

The deselected existing test,
`tests/test_domain.py::test_checkpoint_rejects_future_schema`, expects schema
version 2 to be unsupported. That expectation conflicts with this phase's
intentional checkpoint-v2 support; the test was not changed. The full-suite
failures include that stale expectation plus the 14 pre-existing failures
recorded during Phase 6: import/dependency and SQLite migration expectations,
CLI flag expectations, and tool capability/authorization/artifact behavior.
No tests were changed.

## Persistence and control contracts

`process_ownership` stores a fixed lease ID, owner UUID, process ID, heartbeat,
and expiry. Only the lease owner performs startup recovery and task scheduling.
Owned transactions conditionally extend/fence the lease within the transaction;
the heartbeat serializes with local transactions to avoid competing writes to
the lease record.

Recovery is conservative: replay only read-only/idempotent in-flight tool
invocations, mark uncertain writes `unknown`, and require an independently
observed outcome before reconciliation. Recovery checkpoints live runs as
paused and does not extend deadlines. An interrupted model request is eligible
for one recovery retry. Resume is rejected while unknown tool outcomes remain.
Pause and cancellation are persisted and take effect at safe boundaries.

## Limitations and review points

- MongoDB lease acquisition, heartbeat, fencing, transaction conflict behavior,
  and restart recovery have not been tested against Atlas or another live
  replica-set deployment.
- The legacy schema-v2 test is stale relative to the supported checkpoint
  schema. No test file was altered to hide that mismatch.
- The ownership heartbeat shares the local transaction lock; a live deployment
  test should verify lease behavior during slow transactions and process pauses.
- The process lease is per database, so separate harness applications sharing
  one database intentionally cannot schedule simultaneously.

Phase 7 is implemented and awaiting human review. Do not start Phase 8 without
explicit approval.
