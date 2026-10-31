# Phase tracker

Phases 3 through 8 are implemented and awaiting human review. See the
[Phase 7 review](reviews/phase-07.md) and
[Phase 8 review](reviews/phase-08.md) for recovery and memory details.

Latest user-requested improvement: Ollama `auto` now uses the Gemma Router /
LFM tool-Worker pipeline. Both model calls are persisted independently; the
worker receives only the selected tool's compact handoff and schema. Static
checks pass; the new pipeline has not yet been verified against live Ollama.
The earlier direct-Gemma performance measurement is documented in
[routing verification](reviews/routing-performance.md).

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and project foundation | Complete; advancement approved |
| 1 | Domain contracts and durable state foundation | Complete; advancement approved |
| 2 | Model gateway and single-agent reasoning loop | Complete; advancement approved |
| 3 | Tool loop, policies and local artifacts | Complete; advancement approved |
| 4 | Resource scheduler and concurrency controls | Complete; advancement approved |
| 5 | Agent factory and durable task coordination | Complete; review pending |
| 6 | Hierarchical budgets and completion validation | Implemented; review pending |
| 7 | Full recovery, pause/resume and reconciliation | Implemented; review pending |
| 8 | User memory and context budgeting | Implemented; review pending |
| 9 | Live model adapter and HTTP control interface | Not started |
| 10 | Acceptance, documentation and release readiness | Not started |

Each phase ends with checks and its own review document. Do not advance without
explicit human approval, per implementation.md sections 1 and 16. Phase 8 is
the current completed scope. Do not begin Phase 9 until explicitly approved.
The user asked not to create test files, so existing tests and inline checks
were used. Static validation passed (mypy, changed-file Ruff, formatting, and
compileall).
The Phase 7 targeted existing-test run passed 26 tests; the legacy test that
expects checkpoint schema version 2 to be unsupported was excluded because
Phase 7 now supports version 2. The full existing suite reports 79 passed and
15 failed: the 14 failures already present in Phase 6 plus that stale schema
expectation. File-free schema/transition checks are recorded in the Phase 7
review. MongoDB connectivity and lease/transaction behavior were not tested
against Atlas. Phase 8's exact scope and validation results are recorded in
[the Phase 8 review](reviews/phase-08.md).
