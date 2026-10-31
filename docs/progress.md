# Phase tracker

Phases 3 and 4 are implemented. Phase 5 was explicitly authorized after Phase 4.
Phase 5 is implemented and awaiting human review. See
[Phase 5 review](reviews/phase-05.md) for the handoff and validation evidence.

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
| 6 | Hierarchical budgets and completion validation | Not started |
| 7 | Full recovery, pause/resume and reconciliation | Not started |
| 8 | User memory and context budgeting | Not started |
| 9 | Live model adapter and HTTP control interface | Not started |
| 10 | Acceptance, documentation and release readiness | Not started |

Each phase ends with checks and its own review document. Do not advance without
explicit human approval, per implementation.md sections 1 and 16. Phase 5 is the
only currently authorized implementation scope. The requested project location
is AIOS_HARNESS. Implement code before tests; give installation commands to the
user to execute. The user asked not to create test files; Phase 5 verification
therefore uses inline checks and the existing suite is left untouched. The
MongoDB runtime replacement is implemented but not fully validated: `uv.lock`
is resolved and Python compilation passed, but lint/type checks, the suite, and
live MongoDB behavior remain unverified. No dependency installation or database
connection is authorized by the earlier Phase 1 approval.
