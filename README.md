# Agent Harness

A local, durable agent that can answer questions, inspect and edit files in a
local filesystem and split larger tasks among task-focused workers. With
Ollama, LFM 2.5 Thinking handles ordinary answers and tool calls. Gemma 3 1B
is reserved for content-heavy work such as summaries and kernel-result synthesis.
The harness authorizes and executes tools. Responses appear after
each task completes.

Runs, model steps, tool activity, checkpoints, budgets, and optional memory are
stored in MongoDB. File tools accept paths anywhere on the local filesystem;
write approval can be required for mutations.

## Deterministic agent-orchestration-core runs

`kernel-run` executes a predeclared, strictly validated DAG. The kernel owns
dependencies, capabilities, paging, retries, and bounded parallel admission;
workers receive an isolated task frame and return a result only. Workers never
emit scheduling syscalls or communicate with each other directly.
Their page inputs, private frames, and dependency results flow through
capability-scoped, immutable in-memory key/value entries managed by the kernel.

The ordinary `run` command automatically activates this kernel path for large
requests and when any model frame exceeds its context budget. Recovery covers
tool results, retained history, memory context, delegated tasks, and synthesis.
The one-shot planner sees the task objective and page metadata, then validated
independent workers process page frames concurrently. Configure early admission
with `auto_kernel_trigger_bytes` (default `8192`) or disable automatic recovery
with `auto_kernel_enabled = false`.

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli kernel-run `
  --task "Review this document" `
  --plan examples/kernel-plan.json `
  --payload README.md `
  --user local-user `
  --config examples/ollama.toml
```

Plans are limited to eight acyclic tasks. The runtime rejects malformed plans,
unknown dependency/page references, unauthorized capabilities, and invalid
agent definitions before any worker is scheduled. Independent ready tasks run
concurrently only within the configured task/model/tool resource limits.

## Get started

You need Python 3.11, `uv`, a reachable MongoDB replica set (MongoDB Atlas
works), and Ollama with the configured models available. From this folder:

```powershell
Copy-Item .env.example .env
notepad .env
uv sync --locked --extra dev --python 3.11
ollama pull gemma3:1b
ollama pull lfm2.5-thinking:1.2b
```

Set `AGENT_HARNESS_MONGODB_URI` in `.env` to your own MongoDB connection string.
Keep credentials private and URL-encode reserved characters in the password.
The database defaults to `test`. Do not commit `.env`.
Relative file paths use the process current directory. `run.bat` starts in this
`AIOS_HARNESS` folder. File tools also accept absolute paths elsewhere on disk.

Run one task:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli run --config examples/ollama.toml --task "Explain a queue in one sentence" --user local-user
```

Start an interactive session for multiple tasks:

```powershell
.\run.bat
```

Type `exit` or `quit`, or press Ctrl+C to stop. The interactive session reuses
its Ollama connection. You can also use the `chat` command directly:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli chat --config examples/ollama.toml --user local-user
```

## Tasks to try

Try these prompts in chat. The file examples use the current directory, so
start with read-only requests before asking for a change.

1. **General answer:** “Explain what a queue is in one sentence.”
2. **Find a file:** “List the files in the current directory.”
3. **Read a small section:** “Read lines 1–30 of README.md.”
4. **Search files:** “Find where `model_provider_concurrency` is configured in `C:\Users\dhruba\Downloads\projects\AIOS` and summarize the setting.”
5. **Calculate:** “Calculate 24 × 17 and show the arithmetic.”
6. **Write a file:** “Write two sentences about FIFO to `C:\Users\dhruba\Downloads\projects\AIOS\AIOS_HARNESS\queue-notes.txt`, then read it back.”
7. **Inspect and summarize:** “Read `C:\Users\dhruba\Downloads\projects\AIOS\AIOS_HARNESS\README.md`, find the resource scheduling defaults in `C:\Users\dhruba\Downloads\projects\AIOS\AIOS_HARNESS`, and list the model, tool, and memory capacities with their total.”
8. **Independent subtasks:** “In parallel, find the model concurrency defaults and the tool safety limits. Return each finding with its source file.”
9. **Constrained output:** “Summarize the project in exactly three bullet points.”
10. **Memory (optional):** After enabling memory in `examples/memory.toml`, say “Remember that I prefer concise answers,” then ask “How do I prefer answers?”

Delegation is selected by the model, not forced by prompt length. For the parallel
example, ask for separate, independently verifiable findings. File writes and
edits should be requested only when intended; `require_write_approval = true`
can require an explicit approval before a write proceeds.

## Useful commands

Inspect a run and its child tasks:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli inspect RUN_ID --user local-user
```

View the persisted model/tool trace:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli trace RUN_ID --user local-user
```

The trace can contain prompts, responses, tool arguments, and results; treat it
as private. Pause, cancel, recover, resume, approvals, and memory-management
commands are documented in [components.md](components.md).

## System guide

- [flowchart.md](flowchart.md) — end-to-end architecture and execution flows.
- [components.md](components.md) — components, tools, configuration, persistence,
  safety boundaries, and operations.
- [Architecture notes](docs/architecture.md) · [Durable-state contracts](docs/durable-state.md)
  · [Project progress](docs/progress.md)

For development setup and checks, see [components.md](components.md#development).
