# Deterministic agent-orchestration-core extension review

## Delivered

- Strict one-shot plan compiler for 1--8 task DAGs. It rejects malformed JSON,
  cycles, unknown dependencies/pages, unknown agent definitions, unauthorized
  capabilities, invalid JSON Schemas, and unbounded task/page counts.
- Boundary-aware UTF-8 pager with a forward-progress guard and bounded overlap.
- Capability-scoped in-memory key/value communication. Page frames, private task
  frames, and dependency results are immutable JSON values whose readers are
  granted by the kernel; workers have no direct peer-to-peer channel.
- Kernel-owned scheduling over the existing durable coordinator and fair task
  scheduler. Workers receive their assigned pages, predecessor outputs, and
  capabilities before dispatch; no worker emits scheduler syscalls.
- Typed, immutable result envelopes and a root reducer handoff.
- `kernel-run` CLI workflow and `examples/kernel-plan.json`.
- Automatic admission in the ordinary runner: oversized initial input and
  context-budget failures from prompts, history, tool results, memory, delegated
  tasks, or synthesis are paged and assigned to bounded workers.
- A one-shot JSON DAG proposal receives the objective and page metadata only.
  The plan compiler validates its schema, dependencies, page coverage, worker
  identity, and capability requests before dispatch; invalid proposals use the
  deterministic page plan.
- A guarded synthesis retry reduces oversized worker results once before
  returning a budget failure.

## Runtime contract

```text
validated plan + paged payload
    -> kernel stores frames in scoped in-memory key/value memory
    -> kernel assigns keys/capabilities
    -> dependency-free workers run concurrently within configured limits
    -> result validation and durable commit
    -> dependents become ready
    -> existing root model performs final reduction
```

The control plane is deterministic; model output remains nondeterministic.
Parallel admission is bounded by the existing task, model, and tool limits, so
it does not promise unlimited simultaneous GPU generations.
Prompt bytes are used as a conservative preflight bound because Ollama does not
expose a stable HTTP tokenizer endpoint; provider-reported context overflow is
classified and routed into the same recovery path.
The KV communication layer is intentionally ephemeral. Durable task results are
still retained for audit and restart recovery; a recovery must reconstruct live
kernel memory from committed task records rather than assuming RAM survived.

## Validation

- Focused agent-orchestration-core tests: 13 passed.
- Ruff on changed agent-orchestration-core/application/persistence/interface/test files: passed.
- Mypy on all source files: passed (62 files).
- `compileall`: passed.
- Full suite: 92 passed, 15 failed. The failures are the repository's existing
  stale Phase 1--7 expectations already recorded in `progress.md`; the same 15
  failures occurred before this extension and no agent-orchestration-core test failed.

## Remaining operational check

`kernel-run` needs the configured MongoDB deployment and Ollama models for a
live end-to-end execution. That was not run here because it would depend on the
user's local database/service state.
