# Initial architecture decisions

Status: accepted for the Phase 0 scaffold; subject to human architecture review.
Date: 2026-10-31. Each numbered decision remains independently revisable.

## ADR-001: Standalone boundary

Build an independent distribution with no reference-framework imports, SDKs,
servers, configuration or data dependencies. Use `AIOS_HARNESS` per the explicit
user request. Preserve existing files and do not modify the reference checkout.
Consequence: the harness owns its complete runtime rather than wrapping AIOS.

## ADR-002: Asyncio concurrency

Use Python 3.11+ asyncio and explicit structured task lifetimes in one process.
Separate task admission from resource admission. Blocking adapters later receive
bounded execution capacity; timeout cannot falsely imply thread termination.
Reject thread-per-request and speculative distributed workers.

## ADR-003: SQLite ownership

Use SQLite plus aiosqlite, migrations, foreign keys and WAL where supported.
Serialize transaction units explicitly; persist state/events together and use
optimistic versions. Add enforceable process ownership during recovery work.
SQLite is the authority; in-memory futures are only handles. No external database
is required, at the cost of a single-host scheduling boundary.

## ADR-004: Model and tool ports

Own normalized message, usage, request, response and invocation types. Use narrow
Protocols and injected adapters, with fake adapters first and contract tests.
Persist accepted model decisions and stable tool invocation identities. Validate
arguments and trusted policy before execution. Implement signatures in Phase 1
as needed, avoiding speculative empty interfaces in Phase 0.

## ADR-005: Original implementation

Write original code against the specification. Architectural responsibility names
are not permission to copy the reference implementation. Do not vendor, install,
invoke or import AIOS/Cerebrum. Review source and dependency metadata at each gate.

## ADR-006: Scope and phase gates

Initially exclude distributed execution, untrusted plugins, arbitrary generated
code, shell/browser/VM tools, marketplaces, mandatory embeddings and GPU models.
Implement only the authorized phase, then provide evidence and stop for review.
Only Phase 0 is authorized at this handoff. No placeholders imply working runtime
capabilities. Add dependencies and directories in the phase that needs them.

## ADR-007: Reproducible tooling

Use a src-layout package built with Hatchling, strict mypy, Ruff and pytest.
Resolve released tooling against Python 3.11 using uv; commit uv.lock rather than
guessing dependency versions. Include Hatchling in the development lock so build
tooling is recorded too. Runtime dependencies remain empty until used. The locked
workflow is canonical; unpinned pip editable installation remains supported.
