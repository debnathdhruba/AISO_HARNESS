# Architecture and boundaries

This is the target architecture, not implemented runtime behavior. The governing
specification is `../implementation.md` relative to the project root. The user's
directory instruction overrides its proposed `agent-harness/` directory name.

```text
CLI / HTTP -> application services -> run controller
                                      | coordinator -> factory / registry
                                      | task scheduler
                                      v
                              bounded agent transition
                                      |
                              resource gateway
                         model / tool / memory / artifact
                                      |
                           ports -> concrete adapters

State + events + budgets + checkpoints -> SQLite unit of work
```

Domain objects use dataclasses/enums and have no infrastructure dependencies.
Application code depends on narrow Protocol ports. The composition root creates
adapters and owns explicit startup/shutdown; constructors and imports start no work.
Transport validation uses Pydantic v2 when introduced, not domain inheritance from
HTTP schemas. CLI uses Typer, HTTP uses FastAPI/Uvicorn in its authorized phase.
SQLite access will use aiosqlite, tool schemas a standards-compliant JSON Schema
validator. These dependencies are added and locked only when used. Provider SDKs
remain optional; deterministic fake adapters precede real providers.

## Responsibility and import rules

| Layer | Owns | May depend on |
| --- | --- | --- |
| domain | entities, states, errors, immutable values | standard library |
| ports | typed external capability contracts | domain, standard library |
| application and core services | orchestration, policies, bounded transitions | domain, ports, injected collaborators |
| adapters and persistence | normalization, I/O, transactions | domain, ports, external libraries |
| interfaces | validation, principal resolution, presentation | application services, transport libraries |
| bootstrap | wiring and lifecycle | all concrete components |

No domain imports of SQLite, configuration singletons, transport frameworks or
provider SDKs. No mutable global queues or implicit current user. Model output
cannot confer authorization. Ruff bans reference-framework imports, and the
foundation test audits all package source imports. Add layer-specific enforcement
when layers exist. Phase 1 now enforces domain/port imports with an AST boundary
test: only standard library and domain imports are permitted.

The runner advances one bounded decision; the coordinator owns dependencies and
child relationships. Task admission is round-robin across runs, FIFO within runs.
Resource admission separately bounds queues and provider/tool capacity. Waiting
parents release task slots. Cancellation intent is distinct from confirmed stop.
Phase 4 implements in-process model, tool, artifact and memory admission lanes:
queues reject excess work explicitly, model requests are capped per provider, and
capacity snapshots expose active and queued counts. The blocking-tool adapter
bounds submitted thread work and retains its capacity after caller timeout until
the underlying thread actually returns. It cannot forcibly terminate Python
threads.

SQLite is authoritative in one service process. Explicitly serialize transaction
units, use optimistic state versions, and commit state/events atomically. Never
await external work, approval or queue space inside a transaction. Futures,
connections and callables are transient, never checkpoint data. Persist decisions
and tool intent before effects; interrupted unsafe writes become unknown pending
reconciliation. A filesystem rename is not atomic with a database commit.

Children inherit user identity, narrower capabilities and ancestor budgets.
Reserve budget before external work, reconcile usage, and retain deadlines across
restart/pause. Final prose is only a completion candidate: required actions,
children, artifacts and acceptance checks must settle first. Memory sharing stays
within the same user. Artifact access must enforce workspace and symlink bounds.

Built-in file tools operate only under the configured workspace and cannot access
the artifact storage root or reserved runtime data. They reject
absolute/traversal paths and symlink/reparse-point components, use bounded
UTF-8 text, and keep search results capped. File edits replace one exact match
and fail if it is missing or ambiguous; writes remain non-read-only effects.

Known end-state limits: trusted in-process tools, single-host SQLite ownership,
non-preemptive operations, uncertain provider usage and no universal exactly-once
external effects. Phase 1 implements the durable state foundation described in
[durable-state.md](durable-state.md); scheduling, budget enforcement, completion
policy, effect reconciliation and process ownership remain future work.
