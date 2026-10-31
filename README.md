# Agent Harness

A standalone Python agent harness under phased development. Phases 2 through 5
are implemented; Phase 5 awaits human review. Runs,
model decisions, tool invocations, events and checkpoints persist in MongoDB.

## Run with Python

With Ollama running, from this directory:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli run --config examples/ollama.toml --task "Explain a queue in one sentence" --user local-user
```

For an interactive prompt that accepts multiple tasks, run:

```powershell
.\run.bat
```

Type `exit` or `quit`, or press Ctrl+C, to stop the loop. You can also start it
directly with `.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli chat`;
this defaults to `examples/ollama.toml` and user `local-user`.

Ordinary questions now go straight to Gemma in one model call, capped at 512
response tokens by default. Auto routing uses simple local rules and makes no
extra model call. Arithmetic commands and file/artifact operations use LFM for
tools followed by Gemma for the response. Override an ambiguous route with
`--mode chat` or `--mode tools`. `--mode two_stage` retains the older draft flow.
The resolved route is saved in the run; old runs keep their original behavior.

The default tool set includes `read_file`, `write_file`, `search_files`, and
`edit_file` in addition to calculator and artifact tools. File operations use
the configured `--workspace` directory (default `.`), not the process's
unrestricted filesystem. Artifact storage has its own root (default
`.local/artifacts`, configurable with `artifact_root`) and is not exposed through
file tools. Paths must be relative; traversal and
symlink/reparse-point paths are rejected. File content is UTF-8 and bounded to
16 KiB per read/write/edit. Search uses a literal, case-sensitive query and
returns at most 20 line matches while scanning at most 500 files / 2 MiB.
Search skips common generated directories (`.git`, `.venv`, `node_modules`,
`__pycache__`, `.local`) and reports skipped non-UTF-8 files. Edits require one
exact, unique match. Set `require_write_approval = true` when file writes and
edits should pause for explicit approval.

For the deterministic offline tool demo:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli run --task "artifact demo" --user local-user
```

Check model GPU placement with `ollama ps`. On the tested RTX 3050 laptop, both
models reported `100% GPU`; `nvidia-smi` showed both Ollama runners on the NVIDIA
GPU. Placement is managed by Ollama and can change with available VRAM. The
harness does not install drivers or force unsupported GPU settings.

The project lives in `AIOS_HARNESS` as requested; its distribution is
`agent-harness` and its import package is `agent_harness`. It does not require the
AIOS reference checkout or Cerebrum. The existing `READEME.md` is preserved.

Run state and traces use MongoDB database `test` by default. Configure the
connection in a project-root `.env` file as `AGENT_HARNESS_MONGODB_URI`;
`database_name` can be set there as `AGENT_HARNESS_DATABASE_NAME` or overridden
in TOML/with `--database-name`. The harness loads `.env` automatically without
overriding environment variables already set by the shell. Credentials are
secrets: do not put them in TOML, source control, or run snapshots. Atlas
transactions require a replica-set deployment, as provided by Atlas. Existing
`.local` SQLite files are not migrated or read by the new backend; remove them
yourself if desired.

Create a local `.env` from the safe template, then edit it with a rotated
credential. URL-encode reserved characters in the password (`@` becomes `%40`):

```powershell
Copy-Item .env.example .env
notepad .env
```

The password previously shared in chat was exposed and must be rotated before
use. `.env` is ignored by Git; never commit it.

Inspect a run's persisted model/tool/lifecycle trace with:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli trace RUN_ID --user local-user
```

The trace includes private model prompts/responses and tool arguments/results.
It is kept in the same MongoDB database, not copied to a second trace database.
This is the persisted harness trace, not raw provider network payloads or
intermediate streaming tokens. Restrict database credentials and access.

## Resource scheduling (Phase 4)

Each application process shares bounded model, tool, artifact and memory queues
across its concurrent runs. Defaults allow 2 model operations with a per-provider
limit of 1, 4 tool operations, 2 artifact operations, and 2 reserved memory
operations; each queue also has an explicit finite waiting capacity. Configure
these with `model_concurrency`, `model_provider_concurrency`,
`model_queue_capacity`, `tool_concurrency`, `tool_queue_capacity`,
`artifact_concurrency`, `artifact_queue_capacity`, `memory_concurrency`, and
`memory_queue_capacity` in TOML. Synchronous adapter capacity uses
`blocking_tool_workers` and `blocking_tool_queue_capacity`.
`Application.scheduler.snapshot()` exposes active/queued counts while the
application is running. Excess requests fail with a queue-full error rather than
accumulating without bound.

Trusted synchronous tools use a bounded thread pool. A timeout stops waiting for
the result but does not claim to stop the running thread; its capacity remains in
use until that thread actually finishes.

## Agent delegation (Phase 5)

Runs use the versioned local `coordinator` agent definition. It can delegate a
bounded batch to the registered `researcher`, `writer`, or bounded
`coordinator` definitions through `delegate_tasks`. Child objectives,
capability subsets, acceptance criteria, artifact IDs, dependencies, and
idempotency keys are persisted with their task records. Children inherit the
same user and run deadline; requested tools and step limits can only narrow the
parent's permissions and limits.

Independent ready children may run concurrently, while dependencies execute in
order. The task scheduler admits work round-robin across runs and FIFO within a
run; `task_concurrency` and `task_queue_capacity` set its bounds. Defaults cap
delegation depth, each agent's child count, and each run at 16 children. The
initial child-failure behavior is deterministic fail-fast; automatic child
retries and collect-partial behavior are not enabled. Child results, failures,
and artifact references return to the parent as structured observations.
Children may read only artifact IDs explicitly included in their handoff.

Inspect the durable task tree with `inspect RUN_ID --user USER`. Handoffs are
shown only with `--history` or through the local `trace` command.

## Reproducible development installation

Installation commands are for the user to run. From this directory, with uv installed:

```powershell
uv python install 3.11
uv sync --locked --extra dev --python 3.11
```

Verify the existing environment without resolving or installing packages:

```powershell
.\.venv\Scripts\python.exe -c "import agent_harness; print('agent_harness import OK')"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src/agent_harness
```

Development order: write implementation files before tests. Hand installation
commands to the user instead of executing them. If the environment is missing a
dependency, report the required installation command rather than auto-syncing it.

`uv.lock` pins the resolved development and build dependencies; uv sync installs
this project editable. Initial dependency installation requires internet or a
populated package cache. Runtime persistence requires a reachable MongoDB
deployment and a configured URI. SQLite-specific tests remain available through
the development extra; no tests connect to the configured MongoDB cluster.
Python 3.11 is the validation baseline; the declared minimum is 3.11.
Other interpreter versions are not yet validated.

Standard tooling also works in an activated Python 3.11 virtual environment:
`python -m pip install -e ".[dev]"`. This alternative resolves dependencies afresh;
use the locked uv workflow to reproduce the tested versions. Deliberate dependency
updates use `uv lock --upgrade`, followed by every quality check and lock review.

See [architecture](docs/architecture.md), [decisions](docs/decisions/README.md),
[state contracts](docs/durable-state.md), [phase tracker](docs/progress.md), and
[Phase 1 review](docs/reviews/phase-01.md) and
[Phase 5/MongoDB handoff](docs/reviews/phase-05.md).

## Persisted-state demonstration

This exercises the real persistence adapter, without simulating an agent loop:

```powershell
.\.venv\Scripts\python.exe examples\persisted_state.py
```

The demo creates a fresh run and task in the configured MongoDB database,
transitions them to queued/ready, saves a checkpoint, closes the client and
reopens state. Output contains a generated run UUID, version 1, checkpoint state
version 1 and four ordered events. Repeating the command adds a new run.
