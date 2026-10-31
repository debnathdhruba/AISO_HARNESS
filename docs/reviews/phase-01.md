# Phase 1: Domain contracts and durable state foundation

Status: complete; ready for human review. The user explicitly authorized Phase 1.
Phase 2 has not started and requires a separate approval under implementation.md.

## Completed behavior and files

Implemented frozen RunConfig/Run/Task/Event/Checkpoint contracts, UUID validation,
UTC time checks, immutable JSON objects, full run/task/invocation transition tables,
safe error taxonomy and injected clock/ID ports with system adapters.

SQLite now persists user-scoped run/task snapshots, dependencies, ordered events,
control intent and versioned checkpoints. Transaction ownership/serialization,
optimistic conflicts, atomic state/event writes, rollback, append-only event
triggers, foreign keys, WAL and two ordered migrations are implemented.

New implementation files: `domain/{__init__,entities,states,errors}.py`,
`ports/{__init__,state}.py`, `adapters/{__init__,system}.py`, and
`persistence/{__init__,codec,migrations,sqlite}.py` under `src/agent_harness/`.
Public contracts are documented in [durable-state.md](../durable-state.md).

After implementation, added `tests/conftest.py`, `tests/test_domain.py` and
`tests/test_persistence.py`; updated the foundation tests for dependency and layer
boundaries. Added `examples/persisted_state.py`; updated README, architecture,
ADRs, progress tracker, pyproject, lockfile and .gitignore.

## Verification and actual outcomes

Environment: Windows, Python 3.11.15; aiosqlite 0.22.1. The user explicitly replied
"run it" to the proposed `uv sync --extra dev --python 3.11`; it passed, installed
aiosqlite and updated uv.lock. No other installation command was run in Phase 1.
All verification commands used the existing `.venv\Scripts\python.exe`.

| Command/check | Actual outcome |
| --- | --- |
| `python -m pytest -q` | Passed: 39 tests in 0.50s; none skipped |
| `python -m ruff check .` | Passed: all checks passed |
| `python -m ruff format --check .` | Passed: 24 files already formatted |
| `python -m mypy src/agent_harness` | Passed: 13 source files |
| Persisted-state demo below | Passed: reopened run/task/checkpoint and 4 events |
| Source import/dependency boundaries | Passed in pytest; no AIOS/Cerebrum imports or runtime dependencies |

Intermediate checks found three lint issues and, after adding the mutation guard,
two strict-typing issues. These were fixed and all checks rerun successfully.
Linux, live providers, fresh install and process-kill recovery tests were not run;
they are outside this phase. No unresolved failed checks remain.

Meaningful scenarios covered: all legal/illegal status pairs, immutable terminal
states, unknown-write reconciliation edges, malformed JSON/UUID/UTC values,
atomic rollback, swallowed event-write failure, optimistic conflicts, foreign-key
scope, direct-ID ownership, distinct cancel intent/status, checkpoint revisions,
reopen persistence, paginated event ordering and payload redaction, coroutine
transaction isolation, cancellation rollback, expired/shared transaction handles,
migration of a populated version-1 database, repeat migration and future-schema
rejection. Concurrency tests synchronize with asyncio Events, not timing sleeps.

## Exact local demonstration

From `AIOS_HARNESS`:

```powershell
New-Item -ItemType Directory -Force .local | Out-Null
.\.venv\Scripts\python.exe examples\persisted_state.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src/agent_harness
```

Observed sample run: `5d5e4822-0b8a-487e-a111-de67b41a4c96`, status queued,
version 1, checkpoint state version 1. Events: 1 run.created, 2 task.created,
3 run.transitioned, 4 task.transitioned. The database remains in
`.local/harness.sqlite` for review. Subsequent runs generate new IDs.

## Decisions, limitations and implications

See ADR-008 and [schema/transition notes](../durable-state.md). The requested
AIOS_HARNESS directory is retained. Implementation preceded test creation.
Only entities required for durable state were added; no execution loop, scheduler,
model adapter, tool executor, HTTP endpoint or CLI was introduced.

The state foundation can reopen committed data and reject stale writes. It does
not yet recover in-flight effects, enforce process leadership, authenticate callers,
run acceptance checks or reserve budgets. Config/budget fields are immutable
snapshots now, not enforcement. Terminal records cannot be retried in place;
separate attempts must be added when retry execution is implemented.

Event payloads contain authored metadata only; snapshots remain private local
data. Current principal checks apply to all exposed reads/writes. Cancellation
intent records a request without falsely claiming work has stopped. Invocation
unknown is unresolved and has no automatic execution edge. No reference source
was copied, and no commits, publishing or deployment were performed.

## Review gate and proposed Phase 2

Review entity schemas, complete transition tables and the persisted sample run.
No unresolved design questions block Phase 1. implementation.md Phase 1 states:
"Human review: inspect schemas, transition tables, and a persisted sample run. STOP."

With explicit approval, Phase 2 would add normalized model messages, a scripted
fake provider, context building, a bounded single-agent reasoning loop, persisted
model decisions, step/deadline handling and minimal offline CLI submission and
inspection. It would not add delegation or a live network provider.
