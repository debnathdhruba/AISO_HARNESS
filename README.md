# Agent Harness

A standalone Python agent harness under phased development. **Phase 1:** typed
domain contracts, explicit transitions, user-scoped SQLite transactions, ordered
events, migrations and versioned checkpoints. There is no execution loop, CLI,
model integration, tool execution or scheduling yet.

The project lives in `AIOS_HARNESS` as requested; its distribution is
`agent-harness` and its import package is `agent_harness`. It does not require the
AIOS reference checkout or Cerebrum. The existing `READEME.md` is preserved.

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
.\.venv\Scripts\python.exe examples\persisted_state.py --database .local\phase-01.sqlite
```

The demo creates a fresh run and task, transitions them to queued/ready, saves a
checkpoint, closes the database and reopens it. Output contains a generated run
UUID, version 1, checkpoint state version 1 and four ordered events. Repeating
the command adds a new run, preserving previous runs. `.local/` is git-ignored.
