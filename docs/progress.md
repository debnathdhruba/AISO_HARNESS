# Phase tracker

Phase 1 was explicitly authorized by the user and is complete. Its checks passed
on Windows with Python 3.11.15: 39 tests, Ruff lint/format and strict mypy.
See [the Phase 1 review handoff](reviews/phase-01.md) for evidence and limitations.
Phase 2 is not authorized and has not started.

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and project foundation | Complete; advancement approved |
| 1 | Domain contracts and durable state foundation | Complete; ready for human review |
| 2 | Model gateway and single-agent reasoning loop | Not started; awaiting approval |
| 3 | Tool loop, policies and local artifacts | Not started |
| 4 | Resource scheduler and concurrency controls | Not started |
| 5 | Agent factory and durable task coordination | Not started |
| 6 | Hierarchical budgets and completion validation | Not started |
| 7 | Full recovery, pause/resume and reconciliation | Not started |
| 8 | User memory and context budgeting | Not started |
| 9 | Live model adapter and HTTP control interface | Not started |
| 10 | Acceptance, documentation and release readiness | Not started |

Each phase ends with checks and its own review document. Do not advance without
explicit human approval, per implementation.md sections 1 and 16. The requested
project location is AIOS_HARNESS. Implement code before tests; give installation
commands to the user to execute. For Phase 1, the user explicitly authorized the
requested `uv sync --extra dev --python 3.11` command, which installed aiosqlite
0.22.1 and updated the lockfile. That approval does not authorize future installs.
