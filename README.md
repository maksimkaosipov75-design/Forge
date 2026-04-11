# Forge

TUI-first multi-provider coding CLI — Qwen · Claude · Codex · OpenRouter in one terminal.

Forge gives you one interface for:

- Running coding tasks through multiple AI provider CLIs
- Switching providers and models without leaving the session
- Planning and running ordered multi-agent tasks with AI-driven orchestration
- Asking questions to models mid-task with interactive prompts
- Using Telegram as remote control when away from the terminal

## Screenshots

### Welcome Screen — providers, recent runs, always-visible info bar

![Forge welcome screen](docs/images/forge-welcome.svg)

### Multi-agent Orchestration — per-agent colours, step headers, animated status bar

![Forge orchestration](docs/images/forge-orchestration.svg)

### Live Streaming — real-time output, operation indicators, spinner with model name

![Forge live streaming](docs/images/forge-streaming.svg)

### Plan Preview — AI-built plan with parallel groups, Y/n confirmation

![Forge plan preview](docs/images/forge-diff.svg)

---

## Quick Start

### Requirements

- Python 3.11+
- At least one installed provider CLI or API key:
  - [`qwen`](https://github.com/QwenLM/qwen-agent) — Qwen coding agent
  - [`codex`](https://github.com/openai/codex) — OpenAI Codex CLI
  - [`claude`](https://github.com/anthropics/claude-code) — Claude Code CLI
  - OpenRouter API key — for 200+ models via HTTP (no CLI required)

### Install

```bash
git clone https://github.com/maksimkaosipov75-design/Forge.git
cd Forge
pip install -e .
```

For development tools (pytest, coverage):

```bash
pip install -r requirements-dev.txt
```

### Launch

**Textual TUI** (default):

```bash
forge
```

**Lightweight line shell:**

```bash
forge --shell
```

**Non-interactive one-shot commands:**

```bash
forge run "fix the parser"
forge orchestrate "build a small CLI app"
```

---

## Providers

| Provider   | Type      | Setup                              |
|------------|-----------|------------------------------------|
| `qwen`     | CLI       | Install `qwen` CLI, run `qwen auth` |
| `codex`    | CLI       | Install `codex` CLI, run `codex auth` |
| `claude`   | CLI       | Install `claude` CLI, run `claude auth` |
| `openrouter` | API key | `/auth openrouter` inside Forge    |

### Switching providers

```
/provider claude
/provider openrouter
```

### Switching models

```
/model                        # show current model
/model codex o3               # set model for a provider
/model openrouter             # interactive model picker
```

---

## Core Workflow

### Single-agent run

Type a prompt directly:

```
Refactor the session store and add tests
```

### Orchestration (multi-agent plan)

Preview a plan before running:

```
/plan Build a desktop app with Python parsing, Rust backend, and GTK UI
/run-plan
```

Run directly:

```
/orchestrate Build a small REST API with auth and tests
```

Recover a partially-completed orchestration:

```
/recover
/recover confirm
```

### Interactive model questions

When a model needs a decision mid-task, Forge pauses and shows a styled prompt.
Answer inline — the response is fed back to the model automatically.

---

## Commands Reference

### Session

| Command | Description |
|---------|-------------|
| `/commands` | Show all available commands |
| `/clear` | Clear conversation history |
| `/compact [N\|filter]` | Summarise old history |
| `/history [n]` | Show recent runs |
| `/retry` | Retry last prompt |
| `/expand` | Show full last response |

### Workspace

| Command | Description |
|---------|-------------|
| `/cd <path>` | Change working directory |
| `/cwd` | Show current directory |
| `/diff` | Show files changed since last run |
| `/commit [message]` | Commit changed files via git |
| `/save [filename]` | Save last response to file |
| `/export [md\|txt]` | Export session to file |

### Providers & Models

| Command | Description |
|---------|-------------|
| `/provider <name>` | Switch active provider |
| `/providers` | List available providers and status |
| `/model` | Show current model |
| `/model <provider> <model>` | Set model |
| `/auth <provider>` | Authenticate a provider |

### Orchestration

| Command | Description |
|---------|-------------|
| `/plan <task>` | Build and preview a multi-agent plan |
| `/run-plan` | Execute the last previewed plan |
| `/orchestrate <task>` | Plan and run immediately |
| `/replan` | Rebuild the plan for the last task |
| `/recover` | Resume an interrupted orchestration |

### Status & Metrics

| Command | Description |
|---------|-------------|
| `/status` | Provider health overview |
| `/limits` | Rate limit status |
| `/usage` | Token usage for this session |
| `/metrics` | Aggregated metrics across all runs |
| `/stats` | Per-provider statistics |
| `/todos` | Extract TODOs from last response |

### Remote Control

| Command | Description |
|---------|-------------|
| `/remote-control` | Start Telegram remote control |
| `/remote-control status` | Show remote control state |
| `/remote-control stop` | Stop remote control |
| `/remote-control logs` | Tail remote control logs |

---

## Configuration

Create a `.env` file in the project root (or copy `.env.example`):

```bash
# Provider CLI paths (if not on $PATH)
QWEN_CLI_PATH=qwen
CODEX_CLI_PATH=codex
CLAUDE_CLI_PATH=claude

# OpenRouter API (for /auth openrouter or direct key)
OPENROUTER_API_KEY=

# Rate limiting
RATE_LIMIT_MAX_REQUESTS=20
RATE_LIMIT_WINDOW_SECONDS=3600
MAX_PROMPT_LENGTH=12000

# Telegram remote control (optional)
TELEGRAM_TOKEN=
ALLOWED_USER_IDS=12345,67890

# Optional local health/metrics HTTP server
ENABLE_STATUS_HTTP=0
STATUS_HTTP_HOST=127.0.0.1
STATUS_HTTP_PORT=8089
```

---

## Telegram Remote Control

Forge can expose the current session through a Telegram bot.

Start from inside the CLI:

```
/remote-control
```

Or run the bot as a standalone process:

```bash
python main.py
```

Requires `TELEGRAM_TOKEN` and `ALLOWED_USER_IDS` in `.env`.

Bot commands: `/start`, `/help`, `/status`, `/provider`, `/model`, `/cancel`, `/history`, `/runs`, `/metrics`, `/limits`, `/usage`, `/todos`, `/clear`, `/compact`.

---

## Project Structure

```
forge/
├── bot/                    # Telegram remote control bot
│   ├── handlers/           # Command, callback, file, history, task handlers
│   ├── core.py             # BotCore — dispatcher wiring and main state
│   ├── streaming.py        # Live streaming to Telegram messages
│   ├── formatting.py       # HTML/Markdown formatting for Telegram
│   ├── file_registry.py    # Short-ID registry for Telegram callback data
│   └── ui.py               # Telegram UI helpers (chunks, buttons, previews)
│
├── cli/                    # Terminal user interface
│   ├── commands/           # Individual slash-command implementations
│   ├── app.py              # TUI entry point (Textual app)
│   ├── shell.py            # Lightweight line shell (BridgeShell)
│   ├── ui.py               # Rich-based output, status bar, interactive prompts
│   ├── prompt.py           # Low-level prompt primitives (masked, confirm, text)
│   ├── textual_app.py      # Textual widgets and screens
│   ├── session_actions.py  # Session-level actions (clear, compact, export)
│   └── command_catalog.py  # Command registry and help text
│
├── core/                   # Shared domain logic
│   ├── config.py           # Settings (pydantic-settings + .env)
│   ├── providers.py        # Provider definitions and routing
│   ├── openrouter_catalog.py  # OpenRouter model discovery and caching
│   ├── parser.py           # Stream parser — FORGE_EVENT decoding, categories
│   ├── event_protocol.py   # FORGE_EVENT encode/decode protocol
│   ├── orchestrator.py     # AI and rule-based orchestration planner
│   ├── task_models.py      # Data models (ChatSession, TaskRun, TaskResult, …)
│   ├── session_store.py    # SQLite-backed session persistence
│   ├── process_manager.py  # Provider subprocess lifecycle management
│   ├── file_manager.py     # Working directory and project file tracking
│   ├── provider_status.py  # Provider health checking
│   ├── provider_status_http.py  # Optional HTTP health/metrics server
│   ├── credential_store.py # Encrypted API key storage
│   ├── metrics.py          # Aggregated run metrics
│   ├── rate_limiter.py     # Per-user rate limiting
│   └── security_audit.py   # Prompt safety validation
│
├── runtime/                # Execution engine
│   ├── container.py        # RuntimeContainer — dependency wiring
│   ├── executor.py         # ExecutionService — task execution, file tracking
│   ├── api_backends.py     # HTTP API backends (OpenRouter)
│   └── orchestrator_service.py  # OrchestratorService — multi-agent runs
│
├── tests/                  # Test suite (unittest)
├── docs/                   # Documentation and screenshots
├── scripts/                # Utility scripts
│   ├── build.sh            # Build helper
│   └── install_videograb.sh  # yt-dlp installer
│
├── main.py                 # Telegram bot entrypoint
├── main_codex.py           # Legacy standalone Codex bot
├── bridge_cli.py           # CLI entrypoint shim
├── bot.py                  # Compatibility shim → bot/
└── pyproject.toml
```

---

## Testing

```bash
# Run all tests
python -m pytest tests/ -q

# With coverage
python -m coverage run -m pytest tests/ -q
python -m coverage report

# Syntax check only
python -m compileall -q .
```

---

## What's New in 0.2.0

- **bot/ package** — monolithic `bot.py` split into focused modules under `bot/`
- **OpenRouter real-time streaming** — events arrive mid-response; thinking blocks render as they stream
- **Claude thinking blocks via OpenRouter** — extended delta format parsed and rendered
- **CLI interactive prompts** — styled Rich panels for secrets, confirmations, and model questions
- **Model interaction callback** — models can ask questions mid-task; answers fed back automatically
- **AI-driven planning** — `build_plan()` tries AI orchestrator first, falls back to rule-based
- **Smarter file tracking** — skips `venv/`, `node_modules/`, `__pycache__/` and other noise dirs
- **core/ package** — all shared modules consolidated out of root

---

## License

MIT
