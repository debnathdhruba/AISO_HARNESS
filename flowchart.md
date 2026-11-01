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

## Unified Ollama action, tool, and answer flow

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant CLI
    participant Runner as AgentRunner
    participant Gateway as ModelGateway
    participant Unified as UnifiedLLM
    participant LFM as LFM action model
    participant Tools as ToolExecutor
    participant Gemma as Gemma answer model
    participant DB as MongoDB

    User->>CLI: Submit task
    CLI->>Runner: execute(task)
    Runner->>DB: Persist request step and budget reservation
    Runner->>Gateway: Generate through the unified model interface
    Gateway->>Unified: invoke(messages, tools)
    Unified->>LFM: Task, tool schemas, verified observations
    LFM-->>Unified: Structured answer decision or tool call
    alt Tool call
        Unified-->>Gateway: Typed tool call
        Gateway-->>Runner: Normalized tool-call response
        Runner->>DB: Persist accepted response
        Runner->>Tools: Validate permissions and execute
        Tools->>DB: Persist invocation and result
        Tools-->>Runner: Bounded tool observation
        Runner->>DB: Persist next request and observation
        Runner->>Gateway: Invoke UnifiedLLM with observation
        Gateway->>Unified: Continue composite flow
        Unified->>LFM: Task and observed tool results
        LFM-->>Unified: Answer decision
        Unified->>Gemma: User request, answer context, tool evidence
        Gemma-->>Unified: Final natural-language answer
        Unified-->>Gateway: Composite final response and internal trace
        Gateway-->>Runner: Normalized final response
    else Direct answer
        Unified->>Gemma: User request and LFM answer context
        Gemma-->>Unified: Final natural-language answer
        Unified-->>Gateway: Composite final response and internal trace
        Gateway-->>Runner: Normalized final response
    end
    Runner->>DB: Persist validated completion
    Runner->>CLI: Display answer after task completion
    CLI-->>User: Final status / JSON summary
```

The adapter never executes tools: the durable runner and `ToolExecutor` remain
the authorization and side-effect boundary. Both model calls are recorded in
the step's internal trace; tool arguments and intermediate decisions are not
displayed in the terminal.

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
