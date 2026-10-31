# Phase tracker

Only Phase 0 has implementation authorization. Phase 0 checks passed on Windows
with Python 3.11.15; architecture review and approval of Phase 1 are pending.
See [the review handoff](reviews/phase-00.md) for evidence and limitations.

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and project foundation | Complete; ready for human review |
| 1 | Domain contracts and durable state foundation | Not started; awaiting approval |
| 2 | Model gateway and single-agent reasoning loop | Not started |
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
commands to the user to execute. No installation commands were run during the
verification follow-up.
