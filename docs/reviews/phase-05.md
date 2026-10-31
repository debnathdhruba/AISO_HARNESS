# Phase 5 and MongoDB persistence handoff

## Phase 5: agent factory and task coordination

Implemented versioned local agent definitions, scoped agent instances, bounded
fair task admission, durable child-task batches, dependency ordering, idempotent
delegation, capability/step narrowing, explicit artifact handoffs, and task-tree
inspection. The inline end-to-end delegation smoke check passed: a coordinator
delegated two researchers and a dependent writer; the writer received explicit
artifact IDs and used a handed-off research artifact.

No test files were created or changed. The existing suite remains untouched.

## MongoDB runtime replacement

The application bootstrap now uses the async PyMongo store. MongoDB collections
hold run/task state, dependencies, delegations, lifecycle events, model steps,
tool invocations, approvals, checkpoints, and artifact manifests. The MongoDB
database defaults to `test`; configure the URI through
`AGENT_HARNESS_MONGODB_URI`. The URI is a `SecretStr`, is excluded from run
snapshots, and the previously shared credential was not copied into source,
used, or connected to. Existing SQLite data is not migrated or read. The
legacy SQLite adapter remains for the unchanged SQLite-based development suite.

MongoDB transactions require a replica-set or sharded deployment. A
process-local lock serializes transaction units and event sequencing; ownership
and recovery across multiple service processes remain future work.

## Verification state

- The Phase 5 delegation smoke check passed before the persistence replacement.
- `uv lock` resolved the updated dependency set without the deprecated `srv`
  extra declaration.
- Python `compileall` passed for `src` and `examples`.
- The active environment does not have PyMongo, Ruff, mypy, or pytest-asyncio.
  Pylance diagnostics therefore report unresolved PyMongo imports; the
  configuration file has no Pylance diagnostics.
- No MongoDB server connection, live persistence exercise, linter, type check,
  or full test suite has been run after the backend replacement.

Install the project's locked dependencies locally, then run the checks listed
in the README. A live connection should only be tested after setting a rotated
credential in `AGENT_HARNESS_MONGODB_URI`; no connection was attempted during
this implementation.
