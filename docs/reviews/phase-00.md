# Phase 0: Architecture and project foundation

Status: complete and ready for human review. Phase 1 has not started and is not
authorized. This handoff covers a foundation, not a working agent runtime.

## Delivered scope

Created the src-layout `agent_harness` package and `agent-harness` distribution,
Python >=3.11 metadata, editable packaging, typed-package marker, strict mypy,
Ruff and pytest configuration, dependency lock, documentation and phase tracker.
The public contract is an importable, side-effect-free package. No console command
or application service exists yet; those arrive with their implementation phases.

Files added: `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`,
`README.md`, `src/agent_harness/__init__.py`, `src/agent_harness/py.typed`,
`tests/test_foundation.py`, `docs/architecture.md`, `docs/decisions/README.md`,
`docs/progress.md`, and this review. Existing empty `READEME.md` is preserved.

## Verification evidence

Environment: Windows, CPython 3.11.15, uv 0.11.6. Resolved tools: Hatchling 1.33.0,
mypy 2.4.1, pytest 9.2.0, pytest-asyncio 1.4.1, Ruff 0.17.3. Transitive versions
and registry hashes are recorded in `uv.lock`.

During initial setup, before the user reserved installation commands for themself:

| Command | Actual result |
| --- | --- |
| `uv python install 3.11` | Passed; installed CPython 3.11.15 |
| `uv sync --extra dev --python 3.11` | Passed; resolved and installed 18 packages including editable project |
| `uv sync --locked --extra dev --python 3.11` | Passed; checked 18 packages without lock changes |
| `uv run --locked --extra dev python -m ruff format .` | Passed; formatted one file |

During the authorized verification follow-up, no installations were executed.
Commands below used `.\.venv\Scripts\python.exe` directly:

| Check | Actual result |
| --- | --- |
| Package import and interpreter inspection | Passed; Python 3.11.15, source path within this project |
| `-m pytest` | Passed; 2 tests, no skips, 0.04s test time |
| `-m ruff check .` | Passed; all checks passed |
| `-m ruff format --check .` | Passed; 6 files already formatted |
| `-m mypy src/agent_harness` | Passed; no issues in 1 source file |
| Installed distribution metadata inspection | Passed; version 0.1.0, editable=true, no runtime dependencies |
| AST source import audit via pytest | Passed; no AIOS/Cerebrum imports |
| Parsed uv.lock package-name audit | Passed; no AIOS/Cerebrum packages |

No failed checks remain. Linux, other Python versions, wheel installation and
live provider tests were not run. Phase 0 has no live provider or runtime behavior
to exercise. Strict typing currently checks only the minimal package module and
must continue to pass as implementation grows. No test failures were concealed
by skipping tests.

## Local demonstration

From `AIOS_HARNESS`, using the existing environment:

```powershell
.\.venv\Scripts\python.exe -c "import agent_harness; print('agent_harness import OK')"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src/agent_harness
```

Expected: import OK, two passing tests, clean lint/format, and no mypy issues.
Fresh installation commands are in README.md for the user to execute.

## Decisions, deviations and implications

The user's requested `AIOS_HARNESS` location supersedes the specification's
`agent-harness/` directory name. Package and distribution names are unchanged.
ADRs cover the standalone boundary, asyncio, SQLite, model/tool ports, original
implementation, scope exclusions, and reproducible tooling. Runtime dependencies
are deferred until their phases need them, avoiding unused infrastructure now.

No reference source was copied and no reference requirements were installed.
No execution, network provider, side effects, persistence, recovery or budget
enforcement is implemented yet. Architecture documents describe future contracts,
not guarantees of this scaffold. Default checks are offline. Installation needs
internet or cached packages. No publishing, deployment or commits were performed.

## Review gate and next phase

Please review the layout, technology choices, architecture and phase ordering.
There are no unresolved design questions blocking Phase 0. Section 16 of
implementation.md explicitly says: "Human review: approve directory layout,
technology choices, architecture, and phase ordering. STOP."

Proposed Phase 1: identifiers, minimal domain entities and transition tables,
typed errors, injected clock/ID ports, SQLite migrations/repositories/unit of work,
atomic events and initial versioned checkpoints. Verify transitions, persistence
across reopen, migration, version conflicts and transaction isolation. Write
implementation before tests and hand dependency installation commands to the
user. Start only after explicit Phase 1 approval.
