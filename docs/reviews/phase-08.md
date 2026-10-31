# Phase 8 review handoff

## Scope delivered

- Added immutable memory records with principal, owning agent definition,
  private/shared policy, tags, optional run/task provenance, timestamps, and
  soft deletion.
- Added the MongoDB `memory_records` collection and indexes. Reads always scope
  by user and visibility; private records require the owner definition, shared
  records are visible only to agents for that same user, and update/delete are
  owner-only.
- Added `MemoryService`, deterministic lexical retrieval and the
  `MemoryRetrievalProvider` extension point. Retrieval is bounded by candidate
  count, result count, and rendered context bytes and uses the existing bounded
  memory resource lane.
- Added opt-in `memory_save` tool exposure. It requires an explicit sharing
  choice, saves only on an explicit remember request, includes provenance, and
  uses the tool invocation ID for idempotent retry.
- Added opt-in retrieval injection as provenance-bearing, JSON-encoded
  untrusted context. It is not stored in checkpoints or tool history; duplicate
  record IDs are removed. Existing context construction preserves complete
  tool-call/result groups.
- Added trusted-local CLI memory `save`, `get`, `search`, `update`, and `delete`
  commands, configuration flags, an Ollama example config, and documentation.
- No test files were created or modified. Durable memory uses MongoDB, not
  SQLite, consistent with the user's persistence direction.

## Behavior and demo

```text
MongoDB memory_records
        |
 scoped MemoryService -> bounded MEMORY resource lane -> lexical/provider port
        |                                                    |
 opt-in memory_save tool                          ranked, byte-bounded facts
        |                                                    |
 durable invocation ID                          untrusted context injection
```

With Ollama running and a configured MongoDB URI:

```powershell
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli run --config examples/memory.toml --task "Remember that I prefer concise answers and share this preference with my agents." --user local-user
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli run --config examples/memory.toml --task "How do I prefer answers?" --user local-user
.\.venv\Scripts\python.exe -m agent_harness.interfaces.cli run --config examples/memory.toml --task "How does local-user prefer answers?" --user another-user
```

The first task asks the model to explicitly save a `shared` preference; the
second should retrieve it for the same principal; the third must not see it.
Extraction is an agent decision, so a model must select the `memory_save` tool.
For deterministic direct management, `memory save/search/get/update/delete`
commands expose the same scoped service without a model call.

## Validation

| Check | Result |
| --- | --- |
| `python -m mypy src` | Passed: no issues in 53 source files |
| Changed-source Ruff | Passed |
| Changed-source Ruff format check | Passed |
| `python -m compileall -q src` | Passed |
| File-free scope/retrieval/context smoke checks | Passed: private/shared visibility, cross-user get/update/delete denial, duplicate result removal, retrieval-byte bound, provenance rendering and complete tool-call/result grouping |
| Configuration round-trip and legacy defaults | Passed: memory flags persist; absent flags restore to disabled |
| Memory CLI help | Passed for save/get/search/update/delete |
| Full existing `python -m pytest -q` | 79 passed, 15 failed |
| Live MongoDB/Atlas persistence and authorization checks | Not run |
| Live Ollama demo | Not run |

The full-suite failures are the 14 baseline failures recorded in the Phase 6
handoff plus the existing checkpoint-v2 expectation mismatch. They are listed
in the Phase 7 review. No new test files were added.

## Files and contracts

- `domain/memory.py`: `MemoryRecord`, `MemorySharing`.
- `ports/memory.py`: `MemoryRepository`.
- `memory/retrieval.py`: `MemoryRetrievalProvider`, lexical ranking, context rendering.
- `memory/service.py`: CRUD, scope enforcement, retrieval budgets.
- `persistence/mongo.py`: MongoDB encoding, validation, scoped queries and indexes.
- `agents/runner.py`, `bootstrap.py`, `tools/builtins.py`, `agents/factory.py`,
  `application/runs.py`: retrieval injection and opt-in extraction wiring.
- `domain/model.py`, `config.py`, `interfaces/cli.py`: configuration and local
  memory management commands.
- `README.md`, `docs/durable-state.md`, `docs/architecture.md`,
  `docs/progress.md`, `examples/memory.toml`: operating instructions and status.

## Security, recovery, and limitations

- MongoDB credentials and all saved facts are sensitive. This phase was not
  connected to the database; live query filters/indexes and BSON round trips
  remain unverified.
- Retrieval and extraction default off. Extraction is never automatic for all
  messages or tool outputs. The model's tool selection is not guaranteed; use
  the direct CLI for deterministic saving.
- Shared means cross-agent for the same user, never cross-user. Knowing a
  record ID does not bypass get/update/delete scoping.
- Memory writes are idempotent on a tool retry using the invocation ID. Records
  are separate from checkpoints and use soft deletion.
- Retrieval failure is explicit; configured memory is not silently omitted.
- The default retriever is lexical, not semantic. The protocol is the
  extension point; no vector database or embedding dependency was introduced.
- Context limits are UTF-8 bytes, not tokenizer-exact token counts. The
  existing conservative model budget remains in force.
- This phase does not implement Phase 9 HTTP endpoints/authentication.

Phase 8 is implemented and awaiting human review. Proposed next scope is Phase 9
(optional OpenAI-compatible provider and HTTP control interface); do not start
it without explicit approval.
