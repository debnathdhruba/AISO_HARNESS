# Phase 2: Model gateway and single-agent reasoning loop

Completed and verified before Phase 3. The user's follow-up explicitly authorized
advancement to Phase 3 after Phase 2 completion. The requested local Ollama adapter
is an intentional exception to the original Phase 9 provider timing.

Implemented normalized messages/tool-call identities/usage/finish reasons, model
ports, scripted and stateless offline providers, an explicit local Ollama lifecycle,
context budgeting with intact tool groups, a bounded runner, submission/inspection
services and Typer CLI. Migration 3 adds immutable accepted model decisions with
ordered request/response snapshots. Accepted decisions commit before interpretation;
restart tests prove neither LFM nor Gemma accepted output is regenerated.

The configured flow is LFM decision draft, then Gemma response. Phase 2 cannot
execute tools; action responses fail explicitly. Default retries/corrections are
zero. Run deadlines persist across continuation, both stages consume steps, and
final answers report verified=false. Model thinking traces are discarded.

Files: domain/model.py, protocols/model.py, application/runs.py, agents/runner.py,
models/{context,codec,gateway}.py, adapters/{fake,ollama}.py, config.py, bootstrap.py,
interfaces/cli.py; updates to state ports, SQLite, migrations and packaging;
examples/ollama.toml; tests/test_phase2.py and tests/test_ollama.py plus updated
foundation/migration expectations. Implementation was written before tests.

Verification on Windows/Python 3.11.15:

- `python -m pytest -q`: 60 passed, no skips, 1.45s.
- `python -m ruff check .`: passed after correcting import formatting and one test closure.
- `python -m ruff format --check .`: 44 files already formatted.
- `python -m mypy src/agent_harness`: passed, 29 source files.
- Offline CLI run completed: 5963c229-0f1c-4739-a7ad-d43dee1b3346.
- Live local Ollama run completed: aad1555c-789a-4355-a1b6-e32a3c03979c.
  LFM produced the draft and Gemma returned a one-sentence queue explanation.

Exact reproduction from AIOS_HARNESS, using the existing environment:

```powershell
.\.venv\Scripts\agent-harness.exe run --task "Demonstrate the harness" --user local-user
.\.venv\Scripts\agent-harness.exe run --config examples/ollama.toml --task "Explain a queue in one sentence" --user local-user
.\.venv\Scripts\agent-harness.exe inspect RUN_ID --user local-user
.\.venv\Scripts\agent-harness.exe trace RUN_ID --user local-user
```

Inspect with --history deliberately includes private persisted request/response
text. The default output excludes prompts. Continue uses original models/deadline;
an interrupted request without accepted output fails without automatic retry.
Capabilities come from local /api/show. Ollama HTTP errors and malformed payloads
are normalized and tested with mock HTTP; default tests never call Ollama.

Dependencies installed only after the user's explicit authorization: HTTPX 0.28.1,
Pydantic 2.14.2 and Typer 0.27.3, with tested transitive versions in uv.lock.
The local provider is HTTP-only on loopback, ignores proxy environment settings,
does not download models and does not follow redirects. Cancelling HTTP does not
claim to stop server-side generation. Full ownership/recovery, resource scheduling,
hierarchical budgets, semantic correctness checks and HTTP serving remain later work.
Linux, fresh reinstall and cloud providers were not tested. No publishing or commits.

Ollama API implementation references: [chat](https://docs.ollama.com/api/chat),
[tool calls](https://docs.ollama.com/capabilities/tool-calling), and
[thinking fields](https://docs.ollama.com/capabilities/thinking).

Next authorized scope: Phase 3 tool registry/executor, argument validation, calculator,
artifact boundaries, durable intent, approval records, timeout/uncertain effects
and complete tool-result feedback. Phase 4 remains unauthorized.
