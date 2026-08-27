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


@dataclass(frozen=True)
class Resolution:
    """Which model will answer for a role, or why none will."""

    role: str
    provider: str
    model: str
    ok: bool
    reason: str = ""

    def label(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else self.provider


def describe_helpers(container, workspace) -> str:
    """What the helper roles and the check suite would do right now.

    Resolution touches the local server, so this is a real answer rather than a
    reading of the configuration file.
    """
    lines = ["Roles"]
    for name in sorted(ROLES):
        try:
            resolution = container.delegation.resolve_role(name)
        except Exception as exc:  # pragma: no cover - depends on the machine
            lines.append(f"  {name:<10} could not be resolved: {exc}")
            continue
        if not resolution.ok:
            lines.append(f"  {name:<10} unavailable — {resolution.reason}")
        elif resolution.reason:
            lines.append(f"  {name:<10} {resolution.label()} — {resolution.reason}")
        else:
            lines.append(f"  {name:<10} {resolution.label()}")

    lines += ["", "Checks"]
    try:
        lines += container.verification.describe(workspace)
    except Exception as exc:  # pragma: no cover - depends on the machine
        lines.append(f"  could not be read: {exc}")
    return "\n".join(lines)


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

    def resolve_role(self, role: str) -> Resolution:
        """Turn a configured role into a model that can actually answer.

        Deliberately does not fall back across providers. Sliding from a local
        model to a hosted one because Ollama happens to be down would spend the
        user's money to save them a sentence, and the correct answer when a
        helper is unavailable is for the main agent to do the work itself.
        Falling back *within* the local provider is fine: it stays free.
        """
        provider, model = self.assignment_for(role)

        ready, problem = self._container.provider_is_ready(provider)
        if not ready:
            return Resolution(role, provider, model, ok=False, reason=problem)

        if provider != "local":
            return Resolution(role, provider, model, ok=True)

        # Local models have to be pulled before they can answer, and pulling one
        # is a multi-gigabyte download nobody asked for. Report instead.
        #
        # is_model_installed asks the local server; list_available_models returns
        # the curated catalogue, whose entries carry no installed flag at all. An
        # earlier version of this trusted the catalogue and reported every model
        # as ready while nothing was listening on the port - the kind of false
        # confidence that turns into a confusing failure later.
        if model and self._container.local_model_is_installed(model):
            return Resolution(role, provider, model, ok=True)

        substitute = self._first_installed_local_model()
        if substitute is None:
            return Resolution(
                role, provider, model, ok=False,
                reason=(
                    f"no local model is installed, or the server at "
                    f"{self._settings.LOCAL_LLM_BASE_URL} is not reachable"
                ),
            )

        if model:
            return Resolution(
                role, provider, substitute, ok=True,
                reason=f"{model} is not installed; using {substitute} instead",
            )
        return Resolution(role, provider, substitute, ok=True)

    def _first_installed_local_model(self) -> "str | None":
        """The first catalogue model the local server actually has.

        Records are cached inside the catalogue after the first lookup, so
        asking about several names does not mean several round trips.
        """
        try:
            candidates = [item.name for item in self._container.list_available_models("local")]
        except Exception as exc:
            log.debug("Could not list local models: %s", exc)
            return None
        for name in candidates:
            try:
                if self._container.local_model_is_installed(name):
                    return name
            except Exception as exc:
                log.debug("Could not check whether %s is installed: %s", name, exc)
        return None

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

        resolution = self.resolve_role(role)
        if not resolution.ok:
            return (
                f"The '{role}' helper is unavailable: {resolution.reason}. "
                "Do the work yourself."
            )
        provider, model = resolution.provider, resolution.model

        if notify:
            note = f" — {resolution.reason}" if resolution.reason else ""
            notify(f"🤝 delegating to {role} ({resolution.label()}){note}")

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
