# Orchestrator Roadmap

Status: in progress

## Goal

Evolve the project from a single-task Telegram bridge into a multi-provider execution layer that can:
- choose the right agent for a subtask
- split larger work into smaller steps
- pass artifacts between agents
- present task history, limits, and failures in a clear way

## Principles

- Start with deterministic orchestration before adding an AI planner.
- Keep provider integration uniform: the bot should not care whether it talks to Qwen, Codex, or Claude.
- Build observability before autonomy: history, limits, failure reasons, and events come before complex delegation.
- Keep Telegram and future CLI as interfaces over the same core runtime.

## Phase 1. Foundation

Priority: P0
Status: in progress

What to build:
- add Claude CLI as a third provider
- create a shared provider registry with metadata, labels, and CLI paths
- define a provider contract for run/stream/cancel/status/limits
- add rule-based orchestration models for future decomposition
- standardize comments in English for touched code

Why now:
- orchestration is premature without three real providers
- current logic still has provider-specific assumptions spread through the bot

Deliverables:
- `providers.py`
- `ClaudeProcessManager`
- shared orchestrator plan models
- `/plan <task>` preview command

## Phase 2. Observability

Priority: P0
Status: planned

What to build:
- common task history across providers
- explicit failure reasons
- `/limits`
- richer `/status`
- task/subtask/agent run event chain

Why:
- orchestration is hard to trust without visibility
- provider limits and failures are already user-facing problems

Deliverables:
- `TaskRun`, `SubtaskRun`, `FailureReason` style models
- provider health and limits view

## Phase 3. Orchestrator v1

Priority: P1
Status: planned

What to build:
- rule-based decomposition
- provider routing by task kind
- sequential handoff between subtasks
- final synthesis of outputs

Suggested routing:
- Python scripts, data parsing, lightweight glue: Qwen
- Rust, backend, systems logic: Codex
- GTK/UI/CSS/UX polishing: Claude

Why:
- deterministic routing is cheaper and easier to debug than an AI-first planner

## Phase 4. Orchestrator v2

Priority: P2
Status: planned

What to build:
- AI planner for decomposition proposals
- planner validation rules
- critic/reviewer pass
- fallback routing when one agent fails or is rate-limited

Why:
- adds flexibility only after the base system is observable and stable

## Phase 5. Standalone CLI

Priority: P2
Status: planned

What to build:
- multi-agent local CLI
- remote control via Telegram over the same runtime
- interface inspired by mature agent CLIs

Why:
- the reusable core should power both Telegram and CLI surfaces

## Backlog / Research

Priority: P3
Status: research

Items:
- evaluate Nim only after profiling reveals Python bottlenecks
- compare Claude/Codex/Gemini CLI UX patterns
- profile latency and memory once orchestration is real

## What To Build Next

Recommended immediate sequence:
1. Add Claude CLI support.
2. Centralize provider metadata and supported-provider logic.
3. Add a rule-based orchestration plan preview.
4. Normalize provider status and failure reporting.
5. Only then start true delegated multi-agent execution.
