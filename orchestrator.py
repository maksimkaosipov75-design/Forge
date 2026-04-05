from dataclasses import dataclass, field

from providers import normalize_provider_name


@dataclass
class PlannedSubtask:
    subtask_id: str
    title: str
    description: str
    task_kind: str
    suggested_provider: str
    reason: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class OrchestrationPlan:
    prompt: str
    complexity: str
    strategy: str
    subtasks: list[PlannedSubtask] = field(default_factory=list)


class RuleBasedOrchestrator:
    def __init__(self, available_providers: list[str]):
        self.available_providers = [normalize_provider_name(item) for item in available_providers]

    def build_plan(self, prompt: str) -> OrchestrationPlan:
        text = (prompt or "").strip()
        lowered = text.lower()
        subtasks: list[PlannedSubtask] = []

        if self._matches_any(lowered, "python", "script", "parser", "parsing", "json", "csv", "etl", "scraper"):
            subtasks.append(
                PlannedSubtask(
                    subtask_id="python-data",
                    title="Python and data layer",
                    description="Implement scripts, parsers, or lightweight glue logic.",
                    task_kind="python_data",
                    suggested_provider=self._pick_provider("qwen", "codex"),
                    reason="Qwen is the default fit for Python scripting and data-oriented tasks.",
                )
            )

        if self._matches_any(lowered, "rust", "backend", "api", "core", "engine", "service", "performance"):
            subtasks.append(
                PlannedSubtask(
                    subtask_id="backend-core",
                    title="Backend and core logic",
                    description="Implement strongly typed backend, core services, or systems logic.",
                    task_kind="backend_core",
                    suggested_provider=self._pick_provider("codex", "qwen"),
                    reason="Codex is the preferred fit for backend-heavy and systems-style work.",
                )
            )

        if self._matches_any(lowered, "gtk", "libadwaita", "ui", "ux", "css", "design", "frontend"):
            depends_on = ["backend-core"] if any(item.subtask_id == "backend-core" for item in subtasks) else []
            subtasks.append(
                PlannedSubtask(
                    subtask_id="ui-surface",
                    title="UI surface and polish",
                    description="Implement GTK/UI structure, styling, and user-facing refinements.",
                    task_kind="ui_surface",
                    suggested_provider=self._pick_provider("claude", "qwen", "codex"),
                    reason="Claude is the preferred fit for UI structure, styling, and wording quality.",
                    depends_on=depends_on,
                )
            )

        if not subtasks:
            subtasks.append(
                PlannedSubtask(
                    subtask_id="general",
                    title="General implementation",
                    description="Handle the task as a single unit without decomposition.",
                    task_kind="general",
                    suggested_provider=self._pick_provider("qwen", "codex", "claude"),
                    reason="The request looks compact enough to start with a single agent.",
                )
            )

        complexity = self._estimate_complexity(text, subtasks)
        strategy = self._build_strategy(complexity, subtasks)
        return OrchestrationPlan(
            prompt=text,
            complexity=complexity,
            strategy=strategy,
            subtasks=subtasks,
        )

    @staticmethod
    def _matches_any(text: str, *keywords: str) -> bool:
        return any(keyword in text for keyword in keywords)

    def _pick_provider(self, *preferred: str) -> str:
        for candidate in preferred:
            normalized = normalize_provider_name(candidate)
            if normalized in self.available_providers:
                return normalized
        return self.available_providers[0] if self.available_providers else "qwen"

    @staticmethod
    def _estimate_complexity(prompt: str, subtasks: list[PlannedSubtask]) -> str:
        if len(subtasks) >= 3 or len(prompt) > 500:
            return "complex"
        if len(subtasks) == 2 or len(prompt) > 180:
            return "medium"
        return "simple"

    @staticmethod
    def _build_strategy(complexity: str, subtasks: list[PlannedSubtask]) -> str:
        if len(subtasks) == 1:
            return "single-agent execution is enough; orchestration can stay optional"
        if complexity == "complex":
            return "split into dependent subtasks, execute core layers first, then pass artifacts forward"
        return "split by specialty and keep handoff lightweight between agents"
