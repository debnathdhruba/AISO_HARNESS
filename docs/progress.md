# Phase tracker

Phases 3 through 6 are implemented. Phase 5 is awaiting human review. Phase 6
was explicitly authorized and is awaiting human review; see
[Phase 6 review](reviews/phase-06.md) for implementation and validation details.

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
| 4 | Resource scheduler and concurrency controls | Complete; advancement approved |
| 5 | Agent factory and durable task coordination | Complete; review pending |
| 6 | Hierarchical budgets and completion validation | Implemented; review pending |
| 7 | Full recovery, pause/resume and reconciliation | Not started |
| 8 | User memory and context budgeting | Not started |
| 9 | Live model adapter and HTTP control interface | Not started |
| 10 | Acceptance, documentation and release readiness | Not started |

Each phase ends with checks and its own review document. Do not advance without
explicit human approval, per implementation.md sections 1 and 16. Phase 6 is
the current authorized scope; Phase 7 has not been started. The user asked not
to create test files, so the existing suite and inline checks were used. Static
validation passed (mypy, changed-file Ruff, and compileall). The existing full
suite reports 14 failures and 80 passes; see the Phase 6 review for the failure
categories. MongoDB connectivity and transaction behavior were not tested
against Atlas.
