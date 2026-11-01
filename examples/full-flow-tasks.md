# Full-flow validation tasks

Run these from `AIOS_HARNESS` after MongoDB and Ollama are available. Use a
fresh user such as `test-user` and inspect the printed `run_id` after each task.

## 1. Direct single-agent path

```text
Explain the difference between a process and a thread in three concise bullets.
```

Expected: one short run, no child tasks, completed status.

## 2. Automatic context fan-out

```text
Analyze the following material and produce a concise executive summary. Preserve
the major claims, risks, constraints, and open questions:

[paste 10 KB or more of structured text here]
```

Expected: the normal `run` command automatically activates agent-orchestration-core,
creates independent page workers, executes them concurrently, then runs the root
reducer. Inspect the run trace for multiple child tasks.

## 3. Automatic file-heavy task

```text
Inspect the repository documentation, identify the three most important design
risks, and summarize evidence for each risk with file paths.
```

Expected: tool-capable work, bounded file reads/searches, evidence in the final
answer, using only the explicit file paths needed for the task.

## 4. Calculator and filesystem write

```text
Calculate (1250 * 0.18) + 73, write the calculation and result to
C:\Users\dhruba\Downloads\projects\AIOS\AIOS_HARNESS\calculation.txt, then read the file back.
```

Expected: calculator, local file write, local file read, and the computed result
in the final answer.

## 5. Write approval flow

```text
Write `approval-test.txt` in the project directory containing exactly:
approved writes require explicit confirmation
```

Run this with `require_write_approval = true`.

Expected: the run pauses in `waiting`/approval state. Inspect the invocation,
then approve the exact argument hash and continue the run. Repeat with `deny` and
verify the write does not happen.

## 6. Explicit dependency DAG

Use `examples/kernel-plan.json` with a payload file containing several paragraphs:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli kernel-run `
  --task "Review the supplied document and synthesize the findings" `
  --plan examples/kernel-plan.json `
  --payload README.md `
  --user test-user `
  --config examples/ollama.toml
```

Expected: `inspect` runs first, `summarize` receives its typed dependency result,
and the root reducer produces the final response.

## 7. Pause and resume

Start a long task, then use the run ID:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli pause RUN_ID --user test-user
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli inspect RUN_ID --user test-user
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli resume RUN_ID --user test-user
```

Expected: the run reaches `paused`, preserves its checkpoint, and resumes without
losing committed child results.

## 8. Cancellation

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli cancel RUN_ID --user test-user
```

Expected: the run becomes cancelled and no new child or tool work is admitted.

## 9. User memory isolation

```text
Remember that I prefer concise technical answers and share this preference with
my agents.
```

Then ask:

```text
What answer style do I prefer?
```

Finally repeat the question as another user. Expected: the first user can retrieve
the shared fact; the second user cannot see it.

## 10. Recovery/reconciliation

Pause or interrupt a run during an unsafe write, then inspect and recover it:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli recover RUN_ID --user test-user
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli inspect RUN_ID --user test-user
```

Expected: uncertain external effects are not blindly replayed. Reconcile only
after independently checking whether the effect happened.

## Useful inspection commands

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli inspect RUN_ID --user test-user
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli trace RUN_ID --user test-user
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli events RUN_ID --user test-user
```
