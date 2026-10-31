# Direct response routing and GPU verification

Requested change: skip LFM for ordinary questions and verify RTX 3050 execution.
Implemented before adding routing tests. No installations or GPU/driver changes
were performed for this change.

New runs resolve auto mode at submission using local rules: ordinary prompts use
Gemma directly, while arithmetic commands and artifact/file operations select the
tool flow. Explicit required-tools contracts always select the tool flow. Users
can override with --mode chat/tools; --mode two_stage preserves the former flow.
Routing is intentionally heuristic and does not establish semantic task correctness.
The resolved mode is persisted, and legacy snapshots retain their prior two-stage
behavior. Model authority and tool allowlists remain controlled by configuration.

Gemma responses have a separate default limit of 512 tokens; LFM retains the
configured decision budget. This prevents response limits from truncating LFM's
reasoning. CLI/settings/domain, submission routing, runner and examples were
updated, with README commands. Existing two-stage contract tests now explicitly
request that mode; new tests cover routing, overrides, legacy state and one-call
completion with max_steps=1.

Verification: 87 tests passed, Ruff lint/format passed, strict mypy passed on 38
source files. Live run 05536794-734f-49bd-9e1b-3196ff25f42a completed in 7.27 seconds
including CLI/process overhead and any model loading. SQLite records exactly one
response step using gemma3:1b with a 512-token cap. This is one measurement, not
a guaranteed latency or a controlled before/after benchmark.

Ollama ps reported both installed models at 100% GPU. nvidia-smi identified the
NVIDIA RTX 3050 laptop GPU with 4096 MiB VRAM and two Ollama llama-server processes.
Thus CPU fallback was not the observed cause of delay. After the live check Gemma
used a 2048-token context and approximately 875 MB according to ollama ps. GPU
placement is managed by Ollama and may change with VRAM availability.

Phase 3's native LFM multi-tool reliability investigation is still outstanding;
this routing improvement does not claim to complete that phase or start Phase 4.
