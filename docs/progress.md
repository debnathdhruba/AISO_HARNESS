# Phase tracker

Phases 3 and 4 are implemented. Phase 4's review handoff is in
[phase-04 review](reviews/phase-04.md). Stop for human review; Phase 5 is not
authorized. See [Phase 2 review](reviews/phase-02.md) for the earlier verified
runner baseline.

Latest user-requested improvement: direct Gemma routing for ordinary questions,
explicit mode overrides and a separate response-token limit. Verified with 87
tests plus a live one-call run; both Ollama models were observed at 100% GPU on
the RTX 3050. See [routing verification](reviews/routing-performance.md).

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and project foundation | Complete; advancement approved |
| 1 | Domain contracts and durable state foundation | Complete; advancement approved |
| 2 | Model gateway and single-agent reasoning loop | Complete; advancement approved |
| 3 | Tool loop, policies and local artifacts | Complete; advancement approved |
| 4 | Resource scheduler and concurrency controls | Complete; review pending |
| 5 | Agent factory and durable task coordination | Not started |
| 6 | Hierarchical budgets and completion validation | Not started |
| 7 | Full recovery, pause/resume and reconciliation | Not started |
| 8 | User memory and context budgeting | Not started |
| 9 | Live model adapter and HTTP control interface | Not started |
| 10 | Acceptance, documentation and release readiness | Not started |

Each phase ends with checks and its own review document. Do not advance without
explicit human approval, per implementation.md sections 1 and 16. Phase 4 is the
only currently authorized implementation scope. The requested
project location is AIOS_HARNESS. Implement code before tests; give installation
commands to the user to execute. The user asked not to create test files; Phase 4
verification therefore uses inline smoke checks and the existing suite is left
untouched. For Phase 1, the user explicitly authorized the
requested `uv sync --extra dev --python 3.11` command, which installed aiosqlite
0.22.1 and updated the lockfile. That approval does not authorize future installs.
