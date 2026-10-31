# Architecture and boundaries

This document summarizes the intended architecture and current implementation
boundaries; see `progress.md` for phase status. The governing specification is
`../implementation.md` relative to the project root. The user's directory
instruction overrides its proposed `agent-harness/` directory name.
AIOS is an architectural reference only: its responsibilities are reimplemented
here against standalone contracts. Do not copy, vendor, import, or run AIOS or
Cerebrum components. MongoDB is used for durable runtime state as separately
requested by the user; the specification's original SQLite choice was replaced.

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

State + events + budgets + checkpoints -> MongoDB unit of work
```

Domain objects use dataclasses/enums and have no infrastructure dependencies.
Application code depends on narrow Protocol ports. The composition root creates
adapters and owns explicit startup/shutdown; constructors and imports start no work.
Transport validation uses Pydantic v2 when introduced, not domain inheritance from
HTTP schemas. CLI uses Typer, HTTP uses FastAPI/Uvicorn in its authorized phase.
MongoDB access uses PyMongo's asynchronous client and transactional repository
adapter; tool schemas use a standards-compliant JSON Schema
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

No domain imports of MongoDB, configuration singletons, transport frameworks or
provider SDKs. No mutable global queues or implicit current user. Model output
cannot confer authorization. Ruff bans reference-framework imports, and the
foundation test audits all package source imports. Add layer-specific enforcement
when layers exist. Phase 1 now enforces domain/port imports with an AST boundary
test: only standard library and domain imports are permitted.

The runner advances one bounded decision; the coordinator owns dependencies and
child relationships. Task admission is round-robin across runs, FIFO within runs.
Resource admission separately bounds queues and provider/tool capacity. Waiting
parents release task slots. Cancellation intent is distinct from confirmed stop.
For Ollama auto mode, `llm/router.py` implements a durable Router-Worker flow:
Gemma routes using the full task context without native tool schemas, then LFM
generates a single selected tool call from compact routed data and that tool's
schema. Each model request remains an independent persisted step; tool policy,
execution, and result validation stay in the existing runner and tool executor.
The versioned `worker` agent definition is task-focused rather than paired with
an individual tool. Coordinators can create bounded worker tasks through the
factory/coordinator path; independent children run concurrently within the
configured task and model-provider limits. `delegate_tasks` retains its v1
contract for persisted calls and exposes the worker profile through v2.
Phase 4 implements in-process model, tool, artifact and memory admission lanes:
queues reject excess work explicitly, model requests are capped per provider, and
capacity snapshots expose active and queued counts. The blocking-tool adapter
bounds submitted thread work and retains its capacity after caller timeout until
the underlying thread actually returns. It cannot forcibly terminate Python
threads.

MongoDB is authoritative in one service process. Explicitly serialize transaction
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
UTF-8 text, and keep search results capped. Directory listing is non-recursive
and bounded; range reads cap both scanned and returned bytes to reduce context
pressure. File edits replace one exact match and fail if it is missing or
ambiguous; writes remain non-read-only effects.

Known end-state limits: trusted in-process tools, single-service ownership,
non-preemptive operations, uncertain provider usage and no universal exactly-once
external effects. Phase 7 implements process ownership and startup recovery; its
MongoDB lease and transaction behavior have not been verified against a live
deployment.

## AIOS reference alignment (Phases 4–7)

The reference checkout at `..\AIOS\aios` was inspected to map component
responsibilities. The current harness is an independent implementation, not a
port of the reference code. The AIOS components use synchronous/threaded
syscalls and, in places, Cerebrum or PyOpenAGI APIs; those dependencies and
execution assumptions are intentionally not carried into this project.

| AIOS reference responsibility | AIOS component inspected | Standalone harness implementation | Deliberate difference |
| --- | --- | --- | --- |
| Schedule model, memory, storage, and tool requests | `aios/scheduler/base.py`, `aios/scheduler/fifo_scheduler.py`, `aios/scheduler/rr_scheduler.py` | `scheduling/resources.py:ResourceScheduler`; `agents/scheduler.py:TaskScheduler` | Separate bounded async resource admission from fair child-task scheduling. No AIOS generation time-slicing or worker-thread request queues. |
| Create and activate agents | `aios/syscall/factory.py:AgentFactory` | `agents/factory.py:AgentRegistry` and `AgentFactory`; `application/coordinator.py` | Versioned local definitions and durable task-scoped instances; no remote marketplace loading or downloading. |
| Dispatch tool calls | `aios/tool/manager.py:ToolManager` | `tools/registry.py:ToolRegistry` and `tools/executor.py:ToolExecutor` | Validated and authorized registered tools with persisted invocation state, approvals, and uncertain-effect reconciliation; no implicit MCP subprocess startup. |
| Store agent artifacts | `aios/storage/storage.py:StorageManager` | `artifacts/service.py:ArtifactService`, with durable metadata in MongoDB | Workspace-scoped artifacts and manifests/checksums; not the reference LSFS/Cerebrum storage response path. |
| Route memory operations | `aios/memory/manager.py:MemoryManager` and scheduler memory lane | `memory/service.py:MemoryService`, `memory/retrieval.py`, MongoDB `memory_records`, and `scheduling/resources.py:ResourceKind.MEMORY` | Phase 8 adds MongoDB-backed user/agent-scoped memory with deterministic lexical retrieval and a semantic-provider port; no vector database is required. |
| Budget, completion, restart recovery, and process ownership | No direct equivalent was identified in the inspected scheduler/factory/storage/tool/memory components | MongoDB budget accounts/reservations and completion events in `persistence/mongo.py`; ownership/recovery/checkpoints in the same store; policies in `application/completion.py` | These are harness durability and safety extensions, not copied AIOS components. |

Phase mapping: Phase 4 uses the scheduler responsibilities; Phase 5 uses local
factory/registry and task-coordination responsibilities; Phase 6 adds the
budget/completion mechanisms; Phase 7 adds durable ownership and recovery.
`implementation.md` remains the governing phase contract. The harness must
continue to satisfy its standalone and no-vendoring requirements.
