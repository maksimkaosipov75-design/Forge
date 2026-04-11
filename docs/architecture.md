# Architecture

This document describes the internal structure of Forge and how its components interact.

## Package Layout

```
forge/
├── bot/        Telegram remote control bot
├── cli/        Terminal user interface (TUI + line shell)
├── core/       Shared domain logic
├── runtime/    Execution engine
└── tests/      Test suite
```

---

## Data Flow — Single Task

```
User input (CLI or Telegram)
        │
        ▼
  BridgeShell / BotCore
        │ prompt
        ▼
  ExecutionService.execute_provider_task()
        │
        ├─ file snapshot (before)
        │
        ├─ spawn provider subprocess  ──────────── OR ──── OpenRouterExecutionBackend (HTTP SSE)
        │         │                                                   │
        │    stdout lines → LogParser                        SSE events → parse_sse_line()
        │         │                                                   │
        │    FORGE_EVENTs decoded                           encode_forge_event()
        │         │                                                   │
        │    stream_event_callback(line)  ◄────────────────────────────
        │         │
        │    interaction_callback (if model asks ❓/✅)
        │         │
        │    UI updates (Rich Live / Textual)
        │
        ├─ file snapshot (after) → diff → TaskResult.changed_files
        │
        └─ TaskResult returned
```

---

## core/ — Shared Domain Logic

All modules that multiple packages depend on live in `core/`. Nothing in `core/` imports from `bot/`, `cli/`, or `runtime/`.

| Module | Purpose |
|--------|---------|
| `config.py` | `Settings` model via pydantic-settings, loaded from `.env` |
| `providers.py` | Provider registry: definitions, routing, transport type detection |
| `openrouter_catalog.py` | OpenRouter model discovery, fuzzy search, caching |
| `parser.py` | Stream parser — decodes `FORGE_EVENT` lines, classifies `ActionCategory` |
| `event_protocol.py` | `encode_forge_event` / `decode_forge_event` — the shared wire format |
| `orchestrator.py` | `RuleBasedOrchestrator` and `AIOrchestrator` — plan building |
| `task_models.py` | Dataclasses: `ChatSession`, `TaskRun`, `TaskResult`, `SubtaskRun`, … |
| `session_store.py` | SQLite-backed persistence for sessions and run history |
| `process_manager.py` | Subprocess lifecycle for Qwen, Claude, Codex CLI agents |
| `file_manager.py` | Working directory resolution, project file registry |
| `provider_status.py` | Health checks for each provider |
| `provider_status_http.py` | Optional HTTP server exposing `/health` and `/metrics` |
| `credential_store.py` | Encrypted local storage for API keys |
| `metrics.py` | Aggregated token and run metrics |
| `rate_limiter.py` | Per-user sliding-window rate limiting |
| `security_audit.py` | Prompt safety validation before execution |

---

## runtime/ — Execution Engine

| Module | Purpose |
|--------|---------|
| `container.py` | `RuntimeContainer` — DI wiring; owns all provider runtimes and services |
| `executor.py` | `ExecutionService` — runs a single provider task; manages file snapshots, streaming, interaction callback |
| `api_backends.py` | `OpenRouterExecutionBackend` — HTTP SSE streaming backend; emits FORGE_EVENTs in real time |
| `orchestrator_service.py` | `OrchestratorService` — multi-agent plan execution, subtask scheduling, ETA, recovery |

### RuntimeContainer

`RuntimeContainer` is the central dependency hub. It creates and holds:
- One `ProviderRuntime` per active provider (lazy, started on first use)
- `ExecutionService`, `OrchestratorService`
- `SessionStore`, `CredentialStore`, `MetricsCollector`
- `OpenRouterModelCatalog`

### ExecutionService

`execute_provider_task()` runs one prompt through one provider:

1. **File snapshot** — records `mtime` of all project files (skips `venv/`, `node_modules/`, `__pycache__/`, etc.)
2. **Provider dispatch** — calls the subprocess (CLI providers) or HTTP backend (API providers)
3. **Stream parsing** — each stdout line goes through `LogParser`; actionable FORGE_EVENTs are forwarded to `stream_event_callback`
4. **Interaction detection** — if the parser sees `❓` or `✅` events, `interaction_callback` is called to pause the stream and ask the user
5. **File diff** — compares snapshots to identify changed files
6. **Returns `TaskResult`** with answer text, token counts, changed files, exit code

---

## Event Protocol (FORGE_EVENT)

All inter-process communication between provider subprocesses and Forge uses a line-based protocol:

```
FORGE_EVENT:<type>:<base64-encoded-json-payload>
```

Types: `thinking`, `tool_use`, `tool_result`, `question`, `approval`

CLI providers write these lines to stdout. `LogParser` decodes them and updates `ParserState`. `encode_forge_event` / `decode_forge_event` in `core/event_protocol.py` are the canonical codec.

OpenRouter HTTP backend synthesises equivalent FORGE_EVENTs from SSE deltas.

---

## cli/ — Terminal Interface

```
cli/
├── app.py              Entry point: argparse + launch TUI or shell
├── shell.py            BridgeShell — async REPL loop, slash command dispatch
├── ui.py               CliUi — Rich console output, Live status bar, prompts
├── prompt.py           Low-level prompt primitives (no Rich dependency)
├── textual_app.py      Textual widgets and screens
├── session_actions.py  Clear, compact, export, diff, commit
├── command_catalog.py  Command registry and /commands output
└── commands/           One module per slash command
    ├── run.py          /run, /retry, /expand
    ├── orchestrate.py  /orchestrate, /plan, /run-plan, /recover
    ├── providers.py    /provider, /providers, /status
    ├── model.py        /model
    ├── auth.py         /auth
    ├── smoke.py        /smoke (provider connectivity test)
    └── …
```

### BridgeShell loop

```
read prompt
  │
  ├─ starts with /  →  dispatch to command handler in cli/commands/
  │
  └─ plain text     →  execute_provider_task()
                            │
                      stream_event_callback → CliUi.print_stream_event()
                      interaction_callback  → CliUi.prompt_question/confirm()
```

### Interactive Prompts

`cli/prompt.py` provides three primitives used without Rich:
- `read_masked(prompt)` — echoes `*` per character (POSIX raw mode, falls back to `getpass`)
- `read_confirm(prompt, default)` — `[y/N]` loop
- `read_text(prompt)` — plain readline, returns `None` on empty/Ctrl-C

`CliUi` wraps these in styled Rich `Panel` widgets and handles pausing/resuming the `Live` status bar around interactive input.

---

## bot/ — Telegram Remote Control

```
bot/
├── __init__.py         create_bot_and_setup() — top-level factory
├── core.py             BotCore — aiogram dispatcher, handler wiring, session state
├── streaming.py        Live streaming: chunked Telegram message updates
├── formatting.py       HTML formatting for Telegram (code blocks, diffs, headers)
├── file_registry.py    Short-ID → path registry for inline keyboard callbacks
├── ui.py               UI builders: task buttons, file previews, plan buttons
└── handlers/
    ├── commands.py     /start, /help, /status, /clear, /compact, /limits, …
    ├── callbacks.py    Inline keyboard callback routing
    ├── files.py        /ls, /cat, /cd, /tree, /pwd, /project
    ├── history.py      /history, /runs, /diff, /artifact
    ├── providers.py    /provider, /model, /auth
    ├── tasks.py        Task queue management, cancel
    └── workflow.py     Run, orchestrate, recover — the main execution handlers
```

---

## Orchestration

### Planning

`OrchestratorService.build_plan()` tries two strategies in order:

1. **AI planning** — uses the configured planning provider to generate a structured `OrchestrationPlan` (subtasks with provider assignments and dependencies)
2. **Rule-based fallback** — `RuleBasedOrchestrator` applies keyword heuristics to assign subtasks to providers

### Execution

`OrchestratorService.run_orchestration()` executes subtasks sequentially (respecting dependency order):

- Each subtask runs via `ExecutionService.execute_provider_task()`
- Results are accumulated in `TaskRun.subtask_runs`
- Progress is checkpointed to `SessionStore` for recovery
- `interaction_callback` is threaded through so models can ask questions during orchestration

### Recovery

If an orchestration is interrupted, `/recover` loads the last checkpoint from `SessionStore` and resumes from the first incomplete subtask.

---

## Provider Transport Types

Providers fall into two categories:

| Transport | Providers | Mechanism |
|-----------|-----------|-----------|
| **CLI subprocess** | qwen, codex, claude | `process_manager.py` spawns the CLI; stdout is read line by line |
| **HTTP API** | openrouter | `api_backends.py` sends SSE requests; streaming events forwarded to the same callback chain |

`core/providers.py::is_api_provider(name)` distinguishes the two. `ExecutionService` skips the file snapshot for API providers (they never write local files).

---

## Session Persistence

`SessionStore` (SQLite via stdlib `sqlite3`) stores:
- `ChatSession` — per-session conversation history and provider/model state
- `TaskRun` / `SubtaskRun` — every task execution with results and file diffs
- Orchestration checkpoints for recovery

Location: `.session_data/session_store.sqlite3`

---

## Adding a New Provider

1. Add an entry in `core/providers.py` — provider name, CLI path env var, transport type
2. If CLI: add a `*ProcessManager` subclass in `core/process_manager.py`
3. If API: add an `*ExecutionBackend` subclass in `runtime/api_backends.py`
4. Wire into `RuntimeContainer.build_runtime()` in `runtime/container.py`
5. Add the provider specialty string to `AIOrchestrator.PROVIDER_SPECIALTIES` in `core/orchestrator.py`
