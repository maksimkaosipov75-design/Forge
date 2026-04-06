# Cursor-Style CLI Migration Plan

Status: in progress

## Target State

The project should evolve into an agent runtime platform with:
- a reusable `runtime/` core
- a local CLI as the primary interface
- Telegram as a remote control frontend
- persistent runs, artifacts, retries, review, and orchestration

## Phase 1. Core Extraction

Priority: P0
Status: in progress

Goals:
- create a `runtime/` package
- move provider/session/store bootstrapping out of `bot.py`
- keep Telegram working while the core becomes reusable

Deliverables:
- `runtime/container.py`
- `runtime/__init__.py`
- bot integration through runtime services instead of inline setup helpers

## Phase 2. CLI Bootstrap

Priority: P0
Status: in progress

Goals:
- add a local CLI entrypoint
- support `providers`, `plan`, and workspace-aware bootstrapping
- prove that the runtime is no longer Telegram-only

Deliverables:
- `bridge_cli.py`
- basic argparse commands backed by the runtime container

## Phase 3. Run Graph and Execution Services

Priority: P1
Status: planned

Goals:
- split execution/orchestration logic into explicit runtime services
- introduce node-level run graph instead of flat run history
- prepare parallel execution and node retries

Deliverables:
- `runtime/executor.py`
- `runtime/orchestrator_service.py`
- `runtime/run_graph.py`

## Phase 4. Rich CLI / TUI

Priority: P1
Status: planned

Goals:
- replace raw CLI output with a richer terminal experience
- add live status, current agent, subtask progress, review verdict, and artifact browsing

Deliverables:
- `cli/` package
- rich/textual-based output layer

## Phase 5. Remote Control as First-Class UX

Priority: P2
Status: planned

Goals:
- keep Telegram thin
- surface run controls, retries, artifact browsing, and approvals from the runtime

## Current Recommendation

Build the runtime core first, then grow the CLI, and only after that keep enriching orchestration.
