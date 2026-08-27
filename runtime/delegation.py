"""Handing self-contained work to a cheaper helper agent.

The main agent keeps the thread of the task. When part of it is narrow and
well described — find where something is handled, check a diff for a class of
mistake, write one contained change — it can hand that part to a smaller model
instead of doing it itself.

The saving is mostly context, not tokens per se. Reading twelve files to locate
a function costs the expensive model its whole context window; a local model can
do the same reading and answer in three lines. What comes back is the answer,
not the material it was derived from.

Three things differ between roles, and nothing else:

* which provider and model runs it,
* which tools it is allowed to call,
* what it is told it is for.

A helper never receives ``delegate`` itself. Delegation is one level deep on
purpose: a tree of agents spawning agents is not something the caller can reason
about, and the main agent is already the place where the thread is held.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.providers import normalize_provider_name

log = logging.getLogger(__name__)

ROLE_SEARCH = "search"
ROLE_REVIEW = "review"
ROLE_IMPLEMENT = "implement"

# How long a helper may take before the main agent gives up on it and does the
# work itself. Generous, because a cold local model has to be loaded off disk.
DEFAULT_TIMEOUT_SECONDS = 600

MAX_RESULT_CHARS = 6000


@dataclass(frozen=True)
class RoleDefinition:
    """What a helper of this role may do, and what it is told it is for."""

    name: str
    summary: str
    allowed_tools: tuple[str, ...]
    system_prompt: str


READ_ONLY_TOOLS = ("read_file", "list_directory", "glob_files", "search_in_files")


ROLES: dict[str, RoleDefinition] = {
    ROLE_SEARCH: RoleDefinition(
        name=ROLE_SEARCH,
        summary="locate things in the codebase and report where they are",
        allowed_tools=READ_ONLY_TOOLS,
        system_prompt=(
            "You are a search helper working inside a codebase. Another agent is doing "
            "the real work and has asked you to find something so it does not have to "
            "read the project itself.\n\n"
            "Read whatever you need. Then answer with the findings only: file paths, "
            "line numbers, and the few lines that matter. Do not summarise the project, "
            "do not suggest changes, do not explain what the code does unless asked.\n\n"
            "If you cannot find it, say so plainly and say where you looked. A confident "
            "wrong answer is worse than an admission, because the agent that asked you "
            "will act on it."
        ),
    ),
    ROLE_REVIEW: RoleDefinition(
        name=ROLE_REVIEW,
        summary="check work already done and report what is wrong with it",
        # Deliberately read-only. A reviewer that can also fix what it finds
        # destroys the signal: you can no longer tell whether the work was right
        # or the reviewer rescued it.
        allowed_tools=READ_ONLY_TOOLS,
        system_prompt=(
            "You are reviewing work another agent has done. You cannot change anything, "
            "and you should not try — your job is to say what is wrong, precisely enough "
            "that someone else can fix it.\n\n"
            "Read what you need to form a judgement. Then answer in this shape:\n"
            "  VERDICT: pass | revise | fail\n"
            "  NOTES: what is wrong, one item per line, each with a file and line\n\n"
            "pass means you found nothing worth changing. revise means there are "
            "specific defects listed below. fail means the approach itself is wrong.\n\n"
            "Report only what you can point at. Do not pad the list to look thorough; "
            "an invented objection costs more than a missed one, because it sends good "
            "work back around the loop."
        ),
    ),
    ROLE_IMPLEMENT: RoleDefinition(
        name=ROLE_IMPLEMENT,
        summary="carry out one contained change that has already been decided",
        allowed_tools=READ_ONLY_TOOLS + ("write_file", "edit_file", "bash"),
        system_prompt=(
            "You are implementing one contained change for another agent. The decision "
            "about what to do has been made; you are carrying it out.\n\n"
            "Stay inside what you were asked for. Do not refactor around it, do not fix "
            "unrelated things you notice, do not improve style. The agent that asked you "
            "is holding a larger plan you cannot see, and changes outside your brief will "
            "surprise it.\n\n"
            "When you are done, report what you changed: the files, and one line per "
            "change. If you could not do it, say why rather than doing something else "
            "instead."
        ),
    ),
}


def describe_roles() -> str:
    """One line per role, for the tool description the main agent reads."""
    return "; ".join(f"{name} — {role.summary}" for name, role in ROLES.items())


def parse_assignment(raw: str, fallback_provider: str = "local") -> tuple[str, str]:
    """Split a ``provider:model`` setting into its parts.

    Split on the first colon only: Ollama model names carry a tag after their
    own colon, so ``local:qwen2.5-coder:7b`` has to mean the qwen2.5-coder:7b
    model on the local provider, not something called ``qwen2.5-coder``.
    """
    text = (raw or "").strip()
    if not text:
        return normalize_provider_name(fallback_provider), ""
    provider, _, model = text.partition(":")
    return normalize_provider_name(provider.strip()), model.strip()


class DelegationService:
    """Runs a helper agent for a role, and returns what it said.

    Each run gets its own runtime rather than the session's. A helper must not
    inherit the main agent's conversation, its model, or its tool permissions —
    it is a separate worker that happens to share the working directory.
    """

    def __init__(self, container, settings):
        self._container = container
        self._settings = settings

    def assignment_for(self, role: str) -> tuple[str, str]:
        """Which provider and model runs this role."""
        raw = getattr(self._settings, f"DELEGATE_{role.upper()}", "")
        return parse_assignment(raw)

    async def run(
        self,
        role: str,
        task: str,
        cwd: Path,
        notify: Callable[[str], None] | None = None,
    ) -> str:
        definition = ROLES.get(role)
        if definition is None:
            known = ", ".join(sorted(ROLES))
            return f"Error: unknown role '{role}'. Available roles: {known}."

        brief = (task or "").strip()
        if not brief:
            return "Error: delegate needs a task description."

        provider, model = self.assignment_for(role)
        if provider not in self._container.provider_paths:
            return (
                f"Error: the '{role}' helper is configured to use provider '{provider}', "
                "which is not available. Do the work yourself."
            )

        if notify:
            label = f"{provider}:{model}" if model else provider
            notify(f"🤝 delegating to {role} ({label})")

        # allow_delegation=False is the depth limit: without a callback the
        # helper is never offered the delegate tool in the first place.
        runtime = self._container.build_runtime(
            provider, model_name=model, allow_delegation=False
        )
        manager = runtime.manager

        # The helper's world: its own instructions, its own narrow toolset, and
        # no delegate tool of its own. project_context is where a backend puts
        # its system message; a fresh runtime has none, so this is not
        # overwriting anything the session set up.
        context = "\n\n".join(
            [definition.system_prompt, f"Working directory: {Path(cwd)}"]
        )
        if hasattr(manager, "project_context"):
            manager.project_context = context
        if hasattr(manager, "allowed_tools"):
            manager.allowed_tools = definition.allowed_tools

        try:
            await manager.start()
        except Exception as exc:
            log.warning("Helper %s failed to start: %s", role, exc)
            return f"Error: the '{role}' helper could not start ({exc}). Do the work yourself."

        try:
            await manager.send_command(brief, cwd=Path(cwd))
            answer = (runtime.parser.get_full_response() or "").strip()
        except Exception as exc:
            log.warning("Helper %s failed: %s", role, exc)
            return f"Error: the '{role}' helper failed ({exc}). Do the work yourself."
        finally:
            try:
                await manager.stop()
            except Exception as exc:
                log.debug("Helper %s did not stop cleanly: %s", role, exc)

        if not answer:
            return f"The '{role}' helper returned nothing. Do the work yourself."

        if len(answer) > MAX_RESULT_CHARS:
            answer = answer[:MAX_RESULT_CHARS] + "\n... (helper output truncated)"
        return answer


# The tool as the main agent sees it. It lives here rather than in
# tool_executor's TOOL_DEFINITIONS because it is not always offered: a backend
# advertises it only when it has somewhere to delegate to. Helpers get no
# delegate callback, so they never see this and cannot spawn helpers of their
# own — the depth limit is structural rather than a counter someone has to
# remember to check.
DELEGATE_TOOL_DEFINITION: dict = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "Hand one self-contained piece of work to a cheaper helper agent and get its "
            "answer back. Use this when part of the task is narrow enough to describe in "
            "a sentence or two and does not need the context you are holding — locating "
            "something in the codebase, checking work for a class of mistake, or carrying "
            "out a change you have already decided on. "
            "The helper shares your working directory but not your conversation, so the "
            "task must stand on its own. "
            f"Roles: {describe_roles()}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": sorted(ROLES),
                    "description": "Which kind of helper to use.",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "What the helper should do, written so that someone who cannot see "
                        "your conversation can act on it. Name files and symbols explicitly."
                    ),
                },
            },
            "required": ["role", "task"],
        },
    },
}
