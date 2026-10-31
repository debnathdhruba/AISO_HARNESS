# Phase 4: Resource scheduler and concurrency controls

Status: implementation complete; human review required before Phase 5.
Phase 4 was explicitly authorized after the user confirmed Phase 3 was complete.
No test files were created or changed for this phase, per the user's instruction.

## Implemented

- Added `ResourceScheduler` with bounded model, tool, artifact and memory lanes,
  finite queue lengths, per-provider model limits, queue snapshots, queue-full
  outcomes, queued cancellation, timeout-before-admission distinction, permit
  release on failure/cancellation, and application-shutdown draining.
- Shared one scheduler for every runner created during an application lifetime.
  Model calls and tool/artifact operations are routed through their configured
  resource lane. The memory lane is reserved for the later memory phase because
  the harness does not yet have a memory service.
- Added per-resource concurrency and queue-capacity settings. Model admission is
  keyed by provider; artifact/file tools use the artifact lane.
- Removed the runner-wide execution lock. Independent run IDs can progress
  concurrently; calls for the same run through one runner instance remain
  serialized.
- Added an optional synchronous handler for registered tools and a bounded
  thread-pool adapter. Caller timeout/cancellation does not release a worker
  slot early or falsely claim that the underlying Python thread stopped.
- Application shutdown rejects queued requests, cancels and drains other active
  resource owners, and waits for blocking worker threads before closing SQLite.
- Documented resource configuration and exposed active/queued/capacity state
  through `Application.scheduler.snapshot()`.

## Verification

Passed inline, file-free runtime checks on Windows/Python 3.11:

- Compiled all `src/agent_harness` and `examples` Python files.
- Confirmed active capacity and queue limits, queue-full response, per-provider
  admission, and visible active/queued counts through `ModelGateway`.
- Confirmed a cancelled queued operation never ran.
- Confirmed queued deadline expiry does not start the operation.
- Confirmed worker exceptions release permits and later requests still run.
- Confirmed shutdown settles queued requests, cancels active work, and returns
  with all resource counts at zero.
- Confirmed the blocking adapter retains capacity while a cancelled caller's
  underlying worker thread is still running.
- Completed an offline chat run and a scheduled offline artifact demo with all
  four tool invocations succeeding.

The existing pytest, Ruff and mypy commands could not be run because those
development packages are absent from the current `.venv`; dependencies were not
installed. No live provider request was made for Phase 4.

## Review notes and limits

- Resource limits are process-local. Multiple independent OS processes each
  own their own scheduler; shared/distributed scheduling is outside Phase 4.
- The memory queue is ready but unused until the memory service is implemented.
- Python threads cannot be forcibly terminated safely. Timed-out synchronous
  writes retain the existing uncertain-side-effect behavior.
- `Application.scheduler.snapshot()` is an in-process observation API; there is
  not yet a persistent resource-queue history or HTTP status endpoint.

Phase 5 remains unauthorized. Stop here for human review.
