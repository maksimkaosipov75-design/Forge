# Forge

TUI-first multi-provider coding CLI for Qwen, Codex, and Claude.

Forge gives you one interface for:

- running coding tasks through multiple CLI agents
- switching providers and models without leaving the session
- previewing and executing ordered multi-agent plans
- retrying failed steps, reviewing results, and recovering interrupted runs
- using Telegram as remote control when you need to step away from the terminal

## Screenshots

### Welcome Screen — providers, recent runs, aligned command grid, always-visible info bar

![Forge welcome screen](docs/images/forge-welcome.svg)

### Multi-agent Orchestration — per-agent colours, step headers, animated status bar

![Forge orchestration](docs/images/forge-orchestration.svg)

### Live Streaming — real-time output, operation indicators, spinner with model name

![Forge live streaming](docs/images/forge-streaming.svg)

### Plan Preview — AI-built plan with parallel groups, Y/n confirmation

![Forge plan preview](docs/images/forge-diff.svg)

## Quick Start

### Requirements

- Python 3.11+
- One or more installed and authenticated provider CLIs:
  - [`qwen`](https://github.com/QwenLM/qwen-agent)
  - [`codex`](https://github.com/openai/codex)
  - [`claude`](https://github.com/anthropics/claude-code)

### Install

```bash
git clone https://github.com/maksimkaosipov75-design/Forge.git
cd Forge
python -m pip install -e .
```

`pip install -e .` installs all dependencies and registers the `forge` command.

For development and coverage tools:

```bash
python -m pip install -r requirements-dev.txt
```

### Launch

Textual TUI (default):

```bash
forge
```

Lightweight line shell:

```bash
forge --shell
```

One-shot non-interactive commands:

```bash
forge run "fix the parser"
forge orchestrate "build a small CLI app"
```

## Core Workflow

### Single-agent run

Open Forge and type a prompt:

```
Refactor the session store and add tests
```

### Switch provider

```
/provider codex
/model codex o3
```

### Preview an orchestration plan

```
/plan Build a desktop app with Python parsing, Rust backend, and GTK UI
```

### Run the last previewed plan

```
/run-plan
```

### Run orchestration directly

```
/orchestrate Build a desktop app with Python parsing, Rust backend, and GTK UI
```

### Review the last result

```
/review focus on bugs and missing tests
```

### Recover interrupted orchestration

```
/recover
/recover confirm
```

## Commands

### Session

- `/commands`
- `/clear`
- `/compact [N|filter]`
- `/history [n]`
- `/retry`
- `/expand`

### Workspace

- `/cd <path>`
- `/cwd`
- `/diff`
- `/commit [message]`
- `/save [filename]`
- `/export [md|txt]`

### Providers

- `/provider <name>`
- `/providers`
- `/model`
- `/model <provider> <model>`

### Orchestration

- `/plan <task>`
- `/run-plan`
- `/orchestrate <task>`
- `/replan`
- `/recover`

### Status

- `/status`
- `/limits`
- `/usage`
- `/metrics`
- `/stats`
- `/todos`

### Remote

- `/remote-control`
- `/remote-control status`
- `/remote-control stop`
- `/remote-control logs`

## Configuration

Create a `.env` file in the project root:

```bash
# Provider CLI paths (if not on $PATH)
QWEN_CLI_PATH=qwen
CODEX_CLI_PATH=codex
CLAUDE_CLI_PATH=claude

# Limits
RATE_LIMIT_MAX_REQUESTS=20
RATE_LIMIT_WINDOW_SECONDS=3600
MAX_PROMPT_LENGTH=12000

# Telegram remote control (optional)
TELEGRAM_TOKEN=...
ALLOWED_USER_IDS=12345,67890
```

## Telegram Remote Control

Forge can expose the current session remotely through a Telegram bot.

```
/remote-control
/remote-control status
/remote-control logs
```

Requires `TELEGRAM_TOKEN` and `ALLOWED_USER_IDS` in `.env`.

## Storage

Session state is stored in SQLite under `.session_data/session_store.sqlite3`.

Artifacts and exported run files are written under `.session_data/`.

## Metrics and Health

Optional local HTTP endpoints:

```bash
ENABLE_STATUS_HTTP=1
STATUS_HTTP_HOST=127.0.0.1
STATUS_HTTP_PORT=8089
```

Endpoints: `/health`, `/metrics`

In-session: `/metrics`, `/limits`, `/usage`

## Testing

```bash
python -m pip install -r requirements-dev.txt
python -m coverage run -m unittest discover -s tests -p 'test_*.py' -q
python -m coverage report
```

Syntax check:

```bash
python -m compileall -q .
```

## Status

`v0.1` — practical, polished coding workflow:

- TUI-first interface
- single-agent runs
- ordered multi-agent orchestration
- checkpoints and recovery
- SQLite-backed session persistence
- provider health and metrics

## Limitations

- Orchestration is ordered (not a full DAG scheduler)
- Dynamic replanning is a practical fallback, not guaranteed planning intelligence
- Quality depends on the installed provider CLIs and their auth state

## Roadmap

- dependency-aware orchestration
- dynamic replanning
- cost tracking
- benchmark mode
- improved provider routing

## License

MIT
