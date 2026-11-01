# Phase tracker

Phases 3 through 8 are implemented and awaiting human review. See the
[Phase 7 review](reviews/phase-07.md) and
[Phase 8 review](reviews/phase-08.md) for recovery and memory details.

Latest user-requested improvement: ordinary responses and tool decisions use
LFM; Gemma handles content-heavy summaries, context construction, large inputs,
and kernel-result synthesis. Context-budget failures continue into
agent-orchestration-core paging and parallel workers. Static checks pass; this
routing change has not been verified against live Ollama.

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and project foundation | Complete; advancement approved |
| 1 | Domain contracts and durable state foundation | Complete; advancement approved |
| 2 | Model gateway and single-agent reasoning loop | Complete; advancement approved |
| 3 | Tool loop, policies and local filesystem tools | Complete; advancement approved |
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

## Post-phase agent-orchestration-core extension

The user authorized implementation of the deterministic agent-orchestration-core described in
`../kernalLLMOS.md`. The harness now includes a strict one-shot plan compiler,
boundary-aware pager, typed result envelopes, and a `kernel-run` CLI workflow.
The kernel validates the complete DAG and assigns all page frames and capabilities
before dispatch; it does not accept model-generated scheduler syscalls. Execution
uses the existing durable coordinator, fair task scheduler, ownership lease,
resource limits, and result persistence. This extension is awaiting user review.
See [the agent-orchestration-core review](reviews/agent-orchestration-core.md) for its contracts and checks.

The standard runner now admits oversized requests automatically and sends
context-budget failures through the same kernel, including failures involving
tool results, retained history, memory context, delegated tasks, and synthesis.
It uses `auto_kernel_enabled` and `auto_kernel_trigger_bytes` (default: 8192
bytes) for early admission. The one-shot planner receives only the objective
and page metadata; workers receive validated page frames and capability grants.
