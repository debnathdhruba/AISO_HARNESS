# Agent components and operating guide

This guide describes what each part of the harness owns, which tools the agent
can use, where durable state lives, and how to operate the CLI. The diagrams are
in [flowchart.md](flowchart.md); the project also keeps deeper design records in
[`docs/`](docs/).

## At a glance

The harness is a standalone implementation inspired by component
responsibilities in AIOS. It does not import, vendor, or require the AIOS or
Cerebrum projects.

| Layer | Responsibility | Main package |
| --- | --- | --- |
| Domain | Run/task state, messages, budgets, memory, tool contracts, errors | `domain/` |
| Ports | Interfaces for models, persistence, and memory | `ports/` |
| Application | Run submission, route selection, completion validation, delegation | `application/` |
| Agent runtime | Agent definitions, per-task runner, child-task scheduler | `agents/` |
| Model orchestration | Context sizing, model gateway, unified LFM-action/Gemma-answer adapter | `models/`, `llm/` |
| Tools and artifacts | Tool registration/execution, workspace restrictions, artifact service | `tools/`, `artifacts/` |
| Scheduling | Bounded resource queues and bounded synchronous work | `scheduling/` |
| Persistence | MongoDB production state and SQLite development/legacy adapter | `persistence/` |
| Interfaces and wiring | Typer CLI, configuration, startup/shutdown composition | `interfaces/`, `bootstrap.py`, `config.py` |

### Source component map

| Component | What it owns |
| --- | --- |
| `bootstrap.py` | Composition root: constructs MongoDB, run services, coordinators, schedulers, tools, memory, and model adapter; closes them in lifecycle order |
| `config.py` | Validates TOML, `AGENT_HARNESS_*` environment values, and CLI overrides; produces immutable run settings |
| `domain/entities.py`, `states.py`, `errors.py` | Run/task/event/checkpoint identities, legal state transitions, and typed safe errors |
| `domain/model.py`, `tooling.py`, `budgets.py`, `memory.py` | Provider-neutral model messages, tool contracts, budget values, and memory records |
| `ports/model.py`, `state.py`, `memory.py` | Narrow contracts between orchestration and model/persistence/memory implementations |
| `application/runs.py` | Creates and inspects runs; records pause/cancel intents and validates resume preconditions |
| `application/routing.py` | Selects a run mode with deterministic rules and explicit mode overrides |
| `application/coordinator.py` | Validates, persists, orders, and gathers child tasks and their dependencies |
| `application/completion.py` | Checks final candidate, required actions, child state, artifacts, and JSON Schema |
| `agents/factory.py` | Registers versioned agent definitions and creates capability-scoped task instances |
| `agents/runner.py` | Executes one task's model/tool/checkpoint state machine and persists decisions before effects |
| `agents/scheduler.py` | Fair, bounded admission for runnable delegated tasks |
| `models/gateway.py` | Applies shared model-resource admission and checks normalized response types |
| `models/context.py`, `models/codec.py` | Bound and group model context; serialize persisted steps |
| `llm/router.py` | Parse route decisions, prepare compact tool handoffs, and safely extract answer text from JSON |
| `llm/unified.py` | Expose one Ollama model interface; use LFM for structured action decisions and Gemma for final answers |
| `adapters/ollama.py` | Local Ollama HTTP client, model capability discovery, and response normalization |
| `adapters/fake.py`, `adapters/system.py` | Deterministic offline model and injected clock/UUID implementations |
| `tools/registry.py`, `builtins.py` | Versioned tool schemas/handlers and registration of built-in workspace, artifact, calculator, delegation, and memory tools |
| `tools/executor.py` | Tool-call validation, permission checks, approvals, idempotency, invocation persistence, and observations |
| `tools/workspace.py` | Workspace-relative, bounded file listing/read/range/search/write/edit operations |
| `artifacts/service.py` | Artifact content and manifest lifecycle, checksums, versions, and scoped access |
| `memory/service.py`, `memory/retrieval.py` | Explicit memory CRUD, ownership/sharing checks, lexical ranking, and bounded prompt context |
| `scheduling/resources.py`, `blocking.py` | Bounded async resource queues and bounded synchronous worker pool |
| `persistence/mongo.py` | Production MongoDB collections, indexes, transactions, event append, scheduler lease, budgets, and recovery |
| `persistence/sqlite.py`, `migrations.py` | Local legacy/development adapter and SQLite schema migrations; not used by normal MongoDB composition |
| `interfaces/cli.py` | Interactive/one-shot CLI, run inspection, trace, controls, approvals, recovery, and memory commands |

## A task from input to answer

1. The CLI loads defaults, TOML settings, environment variables, and command-line
   overrides. Run options are snapshotted with the submitted task.
2. `RunService` chooses the routing mode and persists a run plus its root
   coordinator task.
3. The coordinator's versioned definition is resolved by `AgentFactory`.
   `AgentRunner` builds bounded model context, reserves budget, and persists each
   model request before it calls a provider.
4. `ModelGateway` applies resource limits. A provider adapter normalizes its
   response. The runner persists the accepted response before acting on it.
5. If the response calls a tool, `ToolExecutor` checks its schema, capabilities,
   write policy, and execution state. Results become observations for subsequent
   model steps.
6. If the work is substantial, the coordinator may persist child worker tasks.
   The scheduler runs dependency-ready work within configured limits.
7. A final answer is checked against outstanding required tools, children,
   artifacts, and any configured JSON Schema. The run's result/status is
   committed with events.

Model output is untrusted data. It cannot grant permissions or bypass tool
authorization.

## Ollama models and routing

The default Ollama configuration uses the composite `UnifiedLLM` interface:

| Role | Model | Work |
| --- | --- | --- |
| Action and tool decision | `lfm2.5-thinking:1.2b` | Produces a schema-constrained answer decision or a call to one available tool |
| Final response | `gemma3:1b` | Turns the action context and observed tool output into the user-facing answer |

`UnifiedLLM` is the model-facing interface, but it does not execute tools. It
returns a typed tool call to `AgentRunner`; `ToolExecutor` validates the schema,
permissions, and approval policy, then persists and executes the operation.
The next invocation gives the observed result back to LFM. LFM decides when the
work is complete, and Gemma writes the final answer from the verified output.
For a direct-answer request, LFM supplies answer context and Gemma writes the
response without a tool call. Both internal model requests and responses are
included in the persisted step trace.

With Ollama and `routing_mode = "auto"`, the harness uses this unified tool
flow. Clearly requested built-in file operations are independently inferred
and required by the harness, so the model cannot skip a requested read or
substitute an answer. Tool schemas are constrained to the permitted operation;
the executor remains the authorization boundary.

Available modes:

| Mode | Behavior |
| --- | --- |
| `auto` | Uses the unified LFM-action/Gemma-answer flow with Ollama; explicit required tools are enforced |
| `router_worker` | Compatibility mode; Ollama still routes through the unified interface |
| `chat` | Answer without tool calls |
| `tools` | Decision/tool flow through the configured model client |
| `two_stage` | Produce a draft, then ask the response model for a final answer |

LFM output uses an Ollama JSON Schema requiring either a complete answer
decision or a complete tool call. The CLI waits for the task to finish before
displaying the answer; action JSON, tool arguments, and intermediate drafts are not shown.
Interactive chat reuses the Ollama client and connection pool across prompts
and includes the three most recent successful exchanges as follow-up context.

## Agents and task delegation

Agent definitions are local, versioned, and registered in `agents/factory.py`.
The active coordinator may delegate through `delegate_tasks` to generic
task-focused `worker` agents; workers do not delegate further. Older
`researcher`, `writer`, and coordinator definitions remain available to resume
persisted tasks created under earlier contracts.

Delegation is appropriate for complex, multi-part, long, or context-heavy work.
Independent subtasks can be submitted together and run concurrently. The model
chooses whether to delegate, so a request in natural language is guidance, not a
guarantee. Child tasks persist their objectives, dependencies, acceptance
criteria, capabilities, handoff, artifact IDs, deadline, and idempotency key.
Permissions and step limits can only narrow from the parent.

Limits include up to eight children per delegation batch, bounded nesting, and
at most 16 children per run. The coordinator permits up to four active children.
Actual parallelism also depends on task scheduler and model-provider capacity.
By default, required-child failures fail the parent; `partial_result_policy =
"allow"` permits eligible partial results.

## Built-in tools

| Tool | Purpose | Important bounds / policy |
| --- | --- | --- |
| `calculator` | Basic `+ - * / %` arithmetic | Bounded expression length and magnitude; no Python evaluation |
| `list_files` | List one workspace directory | Non-recursive, bounded results; skips generated/internal locations |
| `read_file` | Read a UTF-8 workspace file | Bounded text |
| `read_file_range` | Read a selected line range | Up to 100 lines; bounded scanning and output |
| `search_files` | Find a literal query in workspace files | Case-sensitive; bounded file scan and result count |
| `write_file` | Write a UTF-8 workspace file | Workspace-relative paths; treated as a non-idempotent write |
| `edit_file` | Replace one exact unique match | Fails if the target text is missing or ambiguous |
| `artifact_write` | Save a task artifact and manifest | Idempotent invocation; artifacts can be read by ID |
| `artifact_read` | Retrieve an artifact by ID | Access scoped to authorized run/task and handed-off artifact IDs |
| `artifact_list` | List task artifacts | Scoped artifact metadata |
| `delegate_tasks` | Create bounded worker tasks | Version 2 creates generic workers; version 1 stays available for old persisted calls |
| `memory_save` | Save a user-scoped memory fact | Registered only when memory extraction is enabled; explicit user request required |

Workspace paths are relative to the configured workspace. Traversal and
symlink/reparse-point escapes are rejected, and file tools cannot access the
artifact root. Reads/writes/edits are bounded to 16 KiB of text. Search scans
at most 500 files / 2 MiB and returns at most 20 line matches. Directory
listing is non-recursive and bounded; range reading scans at most 2 MiB and
returns at most 12 KiB. Writes and edits can be held for approval by enabling
`require_write_approval`.

## Scheduling and performance controls

`TaskScheduler` fairly admits child work round-robin across runs and FIFO within
a run. `ResourceScheduler` separately bounds model, tool, artifact, and memory
work. The Ollama HTTP connection-pool size follows the configured provider
concurrency. Synchronous operations use a bounded thread pool; timing out the
wait does not claim that a running Python thread was stopped.

Default limits in `examples/ollama.toml`:

| Capacity | Default |
| --- | ---: |
| Model operations / per-provider model operations | 2 / 2 |
| Tool operations | 4 |
| Artifact operations | 2 |
| Memory operations | 2 |
| Child task operations | 4 |
| Blocking tool workers | 2 |

Each resource also has a finite queue capacity. Full queues reject excess work
with an explicit error rather than accumulating indefinitely. See
`examples/ollama.toml` and `src/agent_harness/config.py` for all configurable
queue, model, output, timeout, budget, retry, and memory settings. Set either
output-token option to `-1` for Ollama's no-per-request output cap, or to a
positive integer for a bounded response. The model context window, run deadline,
and total token budget still apply.

## MongoDB persistence

The application composition root uses MongoDB. Collections are created and
indexed at startup; SQLite files are not migrated or read by the MongoDB
backend. The standard Ollama examples use the database name `test`.

| Collection | What it stores |
| --- | --- |
| `runs` | User-scoped run snapshot, status, model/config snapshot, result, deadline, and control intent |
| `tasks` | Root and child task snapshots, assignment, scope, handoff, and status |
| `task_dependencies` | Durable child dependency edges |
| `delegations` | Idempotent parent-to-child delegation records |
| `steps` | Model requests, accepted responses, failures, stage, and ordinal |
| `invocations` | Tool identity/arguments, execution status, result, or error |
| `approvals` | Approval decision tied to the exact invocation/tool version/arguments |
| `artifacts` | Artifact identity, task/run scope, versions, and integrity manifest |
| `events` | Ordered lifecycle and completion events |
| `checkpoints` | Versioned execution continuation for safe recovery |
| `budget_accounts` | Hierarchical task/run budget totals |
| `budget_reservations` | Idempotent reservation and reconciliation records |
| `memory_records` | Private/shared user memory, tags, provenance, and soft deletion |
| `process_ownership` | Scheduler lease and heartbeat for a database |

Transactions commit state changes and their lifecycle events together. A MongoDB
replica-set deployment is required for transactions. The scheduler lease ensures
only one process owns scheduling for a database at a time.

The `.env` file is loaded without overriding values already set in the shell.
Relevant variables:

```text
AGENT_HARNESS_MONGODB_URI=your-private-connection-string
AGENT_HARNESS_DATABASE_NAME=test
```

Do not put credentials in TOML, source control, or run snapshots. A connection
URI is sensitive. Use `.env.example` as the safe template and protect database
access.

## Run recovery and management

The CLI acquires a MongoDB-backed scheduler lease before startup recovery or
task execution. Recovery validates the task tree, reconciles stale budget
reservations conservatively, and handles interrupted operations. Interrupted
non-idempotent writes are marked `unknown`; check the external effect before
reconciling them. Deadlines are not extended during recovery.

Useful operational commands (replace `RUN_ID`, `INVOCATION_ID`, and `USER`):

```powershell
# Inspect a run, its task tree, and status
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli inspect RUN_ID --user USER

# Read model/tool/lifecycle trace (contains private prompts and tool data)
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli trace RUN_ID --user USER

# Inspect events
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli events RUN_ID --user USER

# Request safe-boundary pause or cancellation
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli pause RUN_ID --user USER
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli cancel RUN_ID --user USER

# Recover an interrupted run, then resume a paused one
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli recover RUN_ID --user USER --config examples/ollama.toml
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli resume RUN_ID --user USER --config examples/ollama.toml

# Reconcile an unknown write only after independently checking its effect
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli reconcile INVOCATION_ID --user USER --outcome succeeded --result-json '{"ok":true}'
```

Approval commands need the exact argument hash shown by `inspect`:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli approve INVOCATION_ID --user USER --argument-hash HASH
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli deny INVOCATION_ID --user USER --argument-hash HASH
```

Inspect CLI help for each command's complete options.

### Optional user memory

Memory retrieval and extraction are off by default. To try both, use
`examples/memory.toml`. A model can save a memory only when the user explicitly
asks it to remember a fact. `private` memory is limited to the same user and
agent definition; `shared` memory is visible to other agent definitions for the
same user. No memory crosses user identities.

Manage records with `memory save`, `memory get`, `memory search`,
`memory update`, and `memory delete`. For example:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli memory save --user local-user --agent coordinator --sharing private --content "Prefers concise answers" --tags preference,style
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli memory search --user local-user --agent coordinator --query "answer style"
```

Retrieval is deterministic lexical ranking; a semantic retriever can implement
the memory port without requiring a vector database.

## Budgets and completion

Before external model/tool work, the harness reserves the corresponding
step/token/tool-call budget and later reconciles actual use when known. Charges
apply to the task and its ancestors. Unknown model usage is charged
conservatively at the reserved amount. Retries consume additional budget.

`max_total_steps`, `max_total_tokens`, and `max_total_tool_calls` set run-wide
limits. Retries default off; bounded transient-provider retries are configurable.
Completion checks can require named committed artifacts and a JSON Schema. A
limited number of corrections may be requested after a failed check.

## Development

Use the locked development environment:

```powershell
uv sync --locked --extra dev --python 3.11
.\.venv\Scripts\python.exe -c "import agent_harness; print('agent_harness import OK')"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src/agent_harness
```

Python 3.11 is the tested baseline; the package minimum is 3.11. SQLite-specific
tests remain available in the development extra; runtime persistence uses
MongoDB.

## Current boundaries

- Local Ollama is the only implemented model provider in the interactive
  configuration; the fake provider supports offline demonstrations.
- Model delegation is a model decision. Independent children can execute in
  parallel, but their work is bounded by configured task and provider limits.
- Tool calls and filesystem effects are not exactly-once across external
  systems. Unknown writes require explicit reconciliation.
- Scheduler ownership is per MongoDB database. Atlas/MongoDB transaction and
  lease behavior should be validated against the deployment in use.
- Ollama controls CPU/GPU model placement; the harness does not install drivers
  or force hardware settings.
