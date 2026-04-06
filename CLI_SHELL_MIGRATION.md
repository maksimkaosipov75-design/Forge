# CLI Shell Migration

## Goal

Move the project from a command-runner CLI to an interactive shell that feels
closer to Claude Code and Cursor-style agent terminals.

## Phase 1: Interactive Shell Foundation

- [x] Keep existing non-interactive commands for scripting.
- [x] Open an interactive shell when `bridge` is launched without arguments.
- [x] Add a home screen with provider, working directory, recent runs, and
  remote-control status.
- [x] Support slash commands inside the shell.

## Phase 2: Remote Control Inside CLI

- [x] Add `/remote-control` to start the Telegram bot automatically.
- [x] Add `/remote-control status` to inspect the background bot state.
- [x] Add `/remote-control stop` to stop the background bot.
- [x] Persist `pid`, `log_path`, and timestamps in `.session_data`.

## Phase 3: Product-Like Shell UX

- [x] Show provider and cwd in the shell home screen.
- [ ] Add a persistent prompt with provider and remote-control state.
- [ ] Add a status panel with limits, queue, and remote-control state.
- [x] Add richer run summaries and a recent activity panel.
- [ ] Add a branded startup page closer to Qwen/Claude CLI.
- [ ] Add a task workspace view with stream/status/output sections.
- [ ] Add provider-themed shell chrome and stronger visual identity.
- [ ] Add ANSI-first visual styling so the shell still looks intentional without `rich`.
- [x] Support contextual shortcuts like `/provider codex` and `/orchestrate`.

## Phase 4: Richer Terminal Runtime

- [ ] Add live streaming views for active tasks.
- [ ] Add orchestration timeline and synthesis/review panels.
- [ ] Add retry/resume flows from the shell.
- [x] Evaluate moving from simple REPL to a `textual` full-screen TUI.
- [ ] Add an optional `textual` shell mode alongside the rich shell.
- [ ] Add slash-command autocomplete with contextual hints and enter-to-accept.
- [ ] Bring `textual` shell command parity closer to the rich shell.

## Notes

- The first implementation should prefer a stable Python REPL-style shell over
  a flashy but fragile TUI.
- Remote control should run as a background subprocess, not inside the shell
  event loop.
