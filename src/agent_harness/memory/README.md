# Memory subsystem

All memory-related implementations and durable storage adapters live under this
package:

- `long_term/` contains persisted user/agent memory retrieval and services.
- `short_term/` contains ephemeral per-run working memory, including the
  capability-scoped agent-orchestration key/value store.
- `backends/` contains MongoDB/SQLite durable adapters and their codecs and
  migrations. These adapters also persist runs, tasks, and other application
  state used to reconstruct memory and execution after restart.

Add future memory types as focused subpackages here, such as `episodic/` or
`semantic/`, with provider interfaces kept in `agent_harness.protocols` and domain
contracts kept in `agent_harness.domain` to avoid dependency cycles.
