# Agent architecture flowcharts

These diagrams describe the current implementation in `src/agent_harness`.
Ollama is local to the configured machine; MongoDB is the durable state store.

## System overview

```mermaid
flowchart TD
    U[User task] --> CLI[Typer CLI]
    CLI --> CFG[Load TOML + environment + CLI overrides]
    CFG --> APP[Application composition root]

    APP --> OWN[Acquire MongoDB scheduler lease]
    OWN --> REC[Recover interrupted runs]
    REC --> RUNS[RunService submits or resumes run]
    RUNS --> STORE[(MongoDB)]
    RUNS --> RUNNER[AgentRunner for root task]
    RUNNER -->|delegated child tasks| SCHED[TaskScheduler]
    SCHED --> CHILD_RUNNER[AgentRunner for worker task]

    RUNNER --> FACTORY[AgentFactory + AgentRegistry]
    CHILD_RUNNER --> FACTORY
    RUNNER --> MODEL_GATEWAY[ModelGateway]
    CHILD_RUNNER --> MODEL_GATEWAY
    RUNNER --> TOOL_EXEC[ToolExecutor]
    CHILD_RUNNER --> TOOL_EXEC
    RUNNER --> MEMORY[MemoryService]
    MODEL_GATEWAY --> LIMITS[Bounded ResourceScheduler]
    TOOL_EXEC --> LIMITS
    MEMORY --> LIMITS
    LIMITS --> OLLAMA[OllamaClient]
    LIMITS --> LOCAL_TOOLS[Workspace + artifact tools]

    RUNNER --> STORE
    CHILD_RUNNER --> STORE
    TOOL_EXEC --> STORE
    MEMORY --> STORE
    STORE --> DATA[Runs, tasks, steps, invocations, events,<br/>checkpoints, budgets, artifacts, memory]
    RUNNER --> RESULT[Validated run result]
    RESULT --> CLI
    CLI --> USER_OUT[Stream final answer + status]
```

The SQLite adapter remains available for development/legacy compatibility; the
normal application composition uses MongoDB. See the collection inventory in
[components.md](components.md#mongodb-persistence).

## Run lifecycle

```mermaid
flowchart TD
    START([CLI receives task]) --> SETTINGS[Load and validate settings]
    SETTINGS --> LEASE[Acquire scheduler lease]
    LEASE --> RECOVERY[Recover stale work]
    RECOVERY --> SUBMIT[Create run and root coordinator task]
    SUBMIT --> MODE{Select run mode}

    MODE -->|Ollama auto| ROUTER[Router-worker]
    MODE -->|chat| CHAT[Response model only]
    MODE -->|tools or required tools| DECIDE[Decision model with tool schemas]
    MODE -->|two_stage| DRAFT[Decision draft, then final response]

    ROUTER --> STEP[Build bounded context and persist request step]
    CHAT --> STEP
    DECIDE --> STEP
    DRAFT --> STEP
    STEP --> RESERVE[Reserve step/token budget]
    RESERVE --> MODEL[ModelGateway admits provider request]
    MODEL --> ACCEPT[Persist accepted model response and usage]
    ACCEPT --> CONTROL{Pause, cancel, deadline,<br/>or terminal state?}
    CONTROL -->|Pause/cancel/expired| SAFE[Persist safe terminal or paused state]
    CONTROL -->|Continue| ACTION{What did the model return?}

    ACTION -->|Tool request| VALIDATE[Validate schema, permissions,<br/>and tool-call limits]
    VALIDATE --> INVOCATION[Persist invocation and approval state]
    INVOCATION --> APPROVAL{Write needs approval?}
    APPROVAL -->|Yes| WAIT[Wait for explicit approval]
    WAIT -->|Approved| EXECUTE[Execute bounded tool]
    WAIT -->|Denied| OBSERVE[Record denial observation]
    APPROVAL -->|No| EXECUTE
    EXECUTE --> RESULT_TOOL[Persist tool result]
    OBSERVE --> STEP
    RESULT_TOOL --> STEP

    ACTION -->|Delegate tasks| CHILD[Coordinator persists child tasks]
    CHILD --> DEP{Dependencies satisfied?}
    DEP -->|Yes| PARALLEL[TaskScheduler runs eligible children<br/>within per-run and global limits]
    DEP -->|No| BLOCKED[Wait for prerequisite child]
    BLOCKED --> DEP
    PARALLEL --> JOIN[Collect child outcomes and artifacts]
    JOIN --> STEP

    ACTION -->|Final answer| CHECK[Validate required tools, children,<br/>artifacts, and result schema]
    CHECK -->|Correction allowed| STEP
    CHECK -->|Passed| FINISH[Persist completed/partial result]
    CHECK -->|Failed or exhausted| FAIL[Persist failure]
    SAFE --> EXIT([Return status])
    FINISH --> EXIT
    FAIL --> EXIT
```

## Ollama router-worker and streaming

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant CLI
    participant Runner as AgentRunner
    participant Gateway as ModelGateway
    participant Gemma as Gemma router
    participant LFM as LFM tool specialist
    participant Tools as ToolExecutor
    participant DB as MongoDB

    User->>CLI: Submit task
    CLI->>Runner: execute(task, on_token)
    Runner->>DB: Persist router request step + budget reservation
    Runner->>Gateway: Generate router decision
    Gateway->>Gemma: Full bounded task context, JSON route format
    Gemma-->>Gateway: NDJSON response chunks
    Gateway-->>Runner: Chunks + normalized final response

    alt Router decides to answer
        Runner->>Runner: Parse JSON answer safely
        Runner-->>CLI: Stream only decoded answer text
        Runner->>DB: Persist accepted decision and validated completion
    else Router selects a tool
        Note over Runner,LFM: Router output and tool arguments are not streamed
        Runner->>DB: Persist route and tool-worker request
        Runner->>Gateway: Generate one selected tool call
        Gateway->>LFM: Compact handoff + selected tool schema
        LFM-->>Gateway: Native tool-call response
        Gateway-->>Runner: Validated tool-call response
        Runner->>DB: Persist accepted step and invocation
        Runner->>Tools: Validate authorization and execute
        Tools->>DB: Persist invocation result / artifact metadata
        Tools-->>Runner: Bounded observation
        Runner->>DB: Persist next router step
        Runner->>Gateway: Route again using observations
    end

    Runner->>DB: Persist terminal state and trace events
    CLI-->>User: Final status / JSON summary
```

Streaming is intentionally limited to user-facing final-answer content.
Intermediate router JSON is decoded incrementally so JSON escapes are rendered
as ordinary text. Tool-call arguments, routing decisions, and intermediate
drafts are not sent to the terminal.

## Delegation and parallel children

```mermaid
flowchart LR
    P[Coordinator task] --> D[delegate_tasks v2]
    D --> V[Validate aliases, objectives,<br/>capabilities and dependencies]
    V --> TX[Persist child tasks + idempotency keys]
    TX --> READY{Child dependencies}
    READY -->|Ready now| QUEUE[Fair task queue<br/>round-robin across runs]
    READY -->|Prerequisite pending| WAIT[Wait for dependency]
    WAIT --> READY
    QUEUE --> F[AgentFactory creates task-scoped worker]
    F --> RUN[AgentRunner executes child]
    RUN --> RG[Shared model/tool resource limits]
    RG --> CHILD_RESULT[Persist child result, failure,<br/>and artifacts]
    CHILD_RESULT --> COMPLETE{All required children settled?}
    COMPLETE -->|No| QUEUE
    COMPLETE -->|Yes| COLLECT[Coordinator collects structured outcomes]
    COLLECT --> P
```

Independent ready workers can run concurrently. Dependencies impose ordering.
Concurrency is bounded by the coordinator's active-child limit, the task
scheduler, and the model provider limit; a request for workers does not bypass
these bounds.

## Persistence, recovery, and control

```mermaid
flowchart TD
    START([Process starts]) --> CONNECT[Connect and ping MongoDB]
    CONNECT --> INDEX[Ensure collections' indexes]
    INDEX --> LEASE[Acquire scheduler ownership lease]
    LEASE --> RECOVER[Validate task tree and recover interrupted work]
    RECOVER --> CLASSIFY{Interrupted operation}
    CLASSIFY -->|Model call| FAIL_MODEL[Mark failed; allow bounded recovery retry]
    CLASSIFY -->|Read-only / idempotent tool| REPLAY[Requeue or reconcile safely]
    CLASSIFY -->|Unknown non-idempotent write| UNKNOWN[Mark invocation unknown]
    RECOVER --> LIVE{Run deadline still valid?}
    LIVE -->|Yes| PAUSE[Checkpoint and pause interrupted work]
    LIVE -->|No| EXPIRE[Fail expired work; do not renew deadline]

    UNKNOWN --> INSPECT[Inspect trace and actual external effect]
    INSPECT --> RECONCILE[Explicit reconcile command records observed result]
    RECONCILE --> RESUME[Resume paused run after recovery]
    PAUSE --> RESUME

    CONTROL[Pause/cancel command] --> INTENT[Persist control intent]
    INTENT --> BOUNDARY[Runner checks at safe execution boundary]
    BOUNDARY --> STATUS[Persist paused/cancelled state and checkpoint]
```

MongoDB transactions make state changes and their lifecycle events durable
together. The scheduler lease fences off competing run workers. The terminal
and inspect/control commands do not make a tool's external side effect atomic
with MongoDB; unknown writes must be checked before reconciliation.
