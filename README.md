# Agent Harness

A standalone Python agent harness under phased development. Phases 2, 3 and 4
are implemented; Phase 4 awaits human review before any Phase 5 work. Runs,
model decisions, tool invocations, events and checkpoints persist in SQLite.

## Run with Python

With Ollama running, from this directory:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli run --config examples/ollama.toml --task "Explain a queue in one sentence" --user local-user
```

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

All run state and trace data use one SQLite database by default:
`.local/harness.sqlite`. Example configurations share this same file, so runs
from either example remain visible together. Override it with `database` in
TOML or `--database` on CLI commands. SQLite may create `-wal` and `-shm`
sidecar files while the database is open; they are part of SQLite's operation,
not extra databases.

Inspect a run's persisted model/tool/lifecycle trace with:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli trace RUN_ID --user local-user
```

The trace includes private model prompts/responses and tool arguments/results.
It is kept locally in the same SQLite database, not copied to a second trace
database. This is the persisted harness trace, not raw provider network payloads
or intermediate streaming tokens. Review access and file permissions accordingly.

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
populated package cache. Import and default tests require no network, credentials,
GPU, external services, or model download. Python 3.11 is the validation baseline;
the declared minimum is 3.11. Other interpreter versions are not yet validated.

Standard tooling also works in an activated Python 3.11 virtual environment:
`python -m pip install -e ".[dev]"`. This alternative resolves dependencies afresh;
use the locked uv workflow to reproduce the tested versions. Deliberate dependency
updates use `uv lock --upgrade`, followed by every quality check and lock review.

See [architecture](docs/architecture.md), [decisions](docs/decisions/README.md),
[state contracts](docs/durable-state.md), [phase tracker](docs/progress.md), and
[Phase 1 review](docs/reviews/phase-01.md).

## Persisted-state demonstration

This exercises the real persistence adapter, without simulating an agent loop:

```powershell
New-Item -ItemType Directory -Force .local | Out-Null
.\.venv\Scripts\python.exe examples\persisted_state.py
```

The demo creates a fresh run and task, transitions them to queued/ready, saves a
checkpoint, closes the database and reopens it. Output contains a generated run
UUID, version 1, checkpoint state version 1 and four ordered events. Repeating
the command adds a new run, preserving previous runs. `.local/` is git-ignored.
