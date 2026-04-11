import copy
import hashlib
import json as _json
import re as _re
import time
from dataclasses import dataclass, field

from providers import normalize_provider_name


@dataclass
class _PlanCacheEntry:
    plan: "OrchestrationPlan"
    created_at: float
    hit_count: int = 0


class PlanCache:
    """In-memory LRU+TTL cache for AI-generated orchestration plans."""
    TTL = 3600.0   # 1 hour
    MAX_SIZE = 20

    def __init__(self):
        self._store: dict[str, _PlanCacheEntry] = {}

    def _key(self, prompt: str, providers: list[str]) -> str:
        normalized = " ".join(prompt.lower().split())
        provider_str = ",".join(sorted(providers))
        return hashlib.sha256(f"{normalized}|{provider_str}".encode()).hexdigest()[:16]

    def get(self, prompt: str, providers: list[str]) -> "OrchestrationPlan | None":
        key = self._key(prompt, providers)
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.created_at > self.TTL:
            del self._store[key]
            return None
        entry.hit_count += 1
        return entry.plan

    def put(self, prompt: str, providers: list[str], plan: "OrchestrationPlan"):
        if len(self._store) >= self.MAX_SIZE:
            oldest = min(self._store, key=lambda k: self._store[k].created_at)
            del self._store[oldest]
        key = self._key(prompt, providers)
        self._store[key] = _PlanCacheEntry(plan=plan, created_at=time.monotonic())

    def clear(self):
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


@dataclass
class PlannedSubtask:
    subtask_id: str
    title: str
    description: str
    task_kind: str
    suggested_provider: str
    reason: str
    depends_on: list[str] = field(default_factory=list)
    # parallel_group: subtasks with the same value run concurrently (0 = first group)
    parallel_group: int = 0


@dataclass
class OrchestrationPlan:
    prompt: str
    complexity: str
    strategy: str
    subtasks: list[PlannedSubtask] = field(default_factory=list)
    # Set by AIOrchestrator when AI planning succeeded
    ai_rationale: str = ""


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


class AIOrchestrator:
    """AI-driven planner: sends a planning prompt to a provider and parses the JSON plan.

    Falls back to RuleBasedOrchestrator on any error.
    """

    _SPECIALTIES = {
        "qwen": "Python, scripting, data processing, general coding",
        "codex": "Rust, backend, systems programming, API design, refactoring",
        "claude": "UI, GTK, CSS, writing, code review, documentation",
        "openrouter": "general purpose, fast reasoning, broad model catalogue",
    }
    # Shared plan cache across all instances (per-process lifetime)
    _cache: PlanCache = PlanCache()

    def __init__(self, available_providers: list[str], fallback: RuleBasedOrchestrator):
        self.available_providers = [normalize_provider_name(p) for p in available_providers]
        self.fallback = fallback

    async def build_plan(
        self,
        prompt: str,
        execution_service,
        session,
        runtime,
        stream_event_callback=None,
    ) -> OrchestrationPlan:
        """Build an AI-driven plan. Falls back to rule-based on any failure."""
        # Check cache before calling AI
        cached = self._cache.get(prompt, self.available_providers)
        if cached is not None:
            plan = copy.deepcopy(cached)
            plan.ai_rationale = (plan.ai_rationale.removesuffix(" [cached]") + " [cached]").strip()
            return plan

        planning_prompt = self._build_planning_prompt(prompt)
        # Save/restore last_task_result so planning doesn't pollute session history
        prev_result = session.last_task_result
        try:
            result = await execution_service.execute_provider_task(
                session=session,
                runtime=runtime,
                provider_name=runtime.provider,
                prompt=planning_prompt,
                stream_event_callback=stream_event_callback,
            )
            if result.exit_code == 0 and result.answer_text.strip():
                plan = self._parse_response(prompt, result.answer_text)
                if plan is not None:
                    self._cache.put(prompt, self.available_providers, plan)
                    return plan
        except Exception:
            pass
        finally:
            session.last_task_result = prev_result
        return self.fallback.build_plan(prompt)

    async def replan_remaining(
        self,
        original_prompt: str,
        completed_subtasks: list,
        failed_subtask,
        execution_service,
        session,
        runtime,
    ) -> "OrchestrationPlan | None":
        """Replan remaining work after a partial failure. Returns None if replanning fails."""
        error = getattr(failed_subtask, "error_text", "") or "unknown error"
        replan_prompt = self._build_replan_prompt(
            original_prompt, completed_subtasks, failed_subtask, error
        )
        prev_result = session.last_task_result
        try:
            result = await execution_service.execute_provider_task(
                session=session,
                runtime=runtime,
                provider_name=runtime.provider,
                prompt=replan_prompt,
            )
            if result.exit_code == 0 and result.answer_text.strip():
                return self._parse_response(original_prompt, result.answer_text)
        except Exception:
            pass
        finally:
            session.last_task_result = prev_result
        return None

    def _build_replan_prompt(
        self,
        original_prompt: str,
        completed_subtasks: list,
        failed_subtask,
        error: str,
    ) -> str:
        available_str = ", ".join(self.available_providers)
        provider_lines = "\n".join(
            f"- {p}: {self._SPECIALTIES.get(p, 'general coding')}"
            for p in self.available_providers
        )
        completed_str = "\n".join(
            f"- [{getattr(s, 'status', '?')}] {getattr(s, 'title', s)} (by {getattr(s, 'provider', '?')})"
            for s in completed_subtasks
        ) or "none"
        failed_title = getattr(failed_subtask, "title", "unknown")
        schema = (
            '{"complexity":"simple|medium|complex","strategy":"one-sentence approach",'
            '"rationale":"why this replanning",'
            '"subtasks":[{"id":"r1","title":"Short action title",'
            '"description":"Specific, actionable instructions",'
            f'"provider":"{self.available_providers[0] if self.available_providers else "qwen"}",'
            '"reason":"why this provider","depends_on":[],"parallel_group":0}]}'
        )
        return (
            "You are a task orchestrator performing DYNAMIC REPLANNING after a failure.\n\n"
            f"Original task:\n{original_prompt}\n\n"
            f"Available providers:\n{provider_lines}\n\n"
            f"Already completed:\n{completed_str}\n\n"
            f"Failed step: {failed_title}\n"
            f"Error: {error[:400]}\n\n"
            "Replan ONLY the REMAINING work to complete the original task.\n"
            "Do NOT repeat steps that already succeeded.\n"
            f"Output exactly this JSON structure:\n{schema}\n\n"
            "Rules:\n"
            "- 1 to 3 subtasks maximum\n"
            "- parallel_group: same integer = run concurrently\n"
            f"- only use these providers: {available_str}\n"
            "- OUTPUT JSON ONLY — no other text, no code fences"
        )

    def _build_planning_prompt(self, prompt: str) -> str:
        available_str = ", ".join(self.available_providers)
        provider_lines = "\n".join(
            f"- {p}: {self._SPECIALTIES.get(p, 'general coding')}"
            for p in self.available_providers
        )
        schema = (
            '{"complexity":"simple|medium|complex","strategy":"one-sentence approach",'
            '"rationale":"why this decomposition",'
            '"subtasks":[{"id":"s1","title":"Short action title",'
            '"description":"Specific, actionable instructions for this agent",'
            f'"provider":"{self.available_providers[0] if self.available_providers else "qwen"}",'
            '"reason":"why this provider","depends_on":[],"parallel_group":0}]}'
        )
        return (
            "You are a task orchestrator. Output ONLY valid JSON — no markdown, no explanation.\n\n"
            f"Available providers:\n{provider_lines}\n\n"
            f"Task:\n{prompt}\n\n"
            f"Output exactly this JSON structure:\n{schema}\n\n"
            "Rules:\n"
            "- 1 to 3 subtasks maximum\n"
            "- parallel_group: subtasks sharing the same integer run concurrently; "
            "increment the integer for sequential dependencies\n"
            "- depends_on: list of subtask IDs that must finish before this one starts\n"
            "- complexity: 'simple' for 1 subtask, 'medium' for 2, 'complex' for 3\n"
            f"- only use these providers: {available_str}\n"
            "- descriptions must be specific and actionable, not vague\n"
            "- OUTPUT JSON ONLY — no other text, no code fences"
        )

    def _parse_response(self, original_prompt: str, text: str) -> OrchestrationPlan | None:
        data = self._extract_json(text)
        if not isinstance(data, dict) or "subtasks" not in data:
            return None
        subtasks: list[PlannedSubtask] = []
        for item in data.get("subtasks", []):
            if not isinstance(item, dict):
                continue
            raw_provider = item.get("provider", "")
            provider = normalize_provider_name(raw_provider)
            if provider not in self.available_providers and self.available_providers:
                provider = self.available_providers[0]
            subtasks.append(PlannedSubtask(
                subtask_id=str(item.get("id", f"s{len(subtasks) + 1}")),
                title=str(item.get("title", "Subtask"))[:100],
                description=str(item.get("description", ""))[:600],
                task_kind=str(item.get("task_kind", "general")),
                suggested_provider=provider,
                reason=str(item.get("reason", ""))[:200],
                depends_on=[str(d) for d in item.get("depends_on", [])],
                parallel_group=int(item.get("parallel_group", 0)),
            ))
        if not subtasks:
            return None
        complexity = str(data.get("complexity", "medium"))
        if complexity not in ("simple", "medium", "complex"):
            complexity = "medium"
        return OrchestrationPlan(
            prompt=original_prompt,
            complexity=complexity,
            strategy=str(data.get("strategy", ""))[:300],
            subtasks=subtasks,
            ai_rationale=str(data.get("rationale", ""))[:400],
        )

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        text = text.strip()
        # Strip markdown code fences if present
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
        if m:
            text = m.group(1)
        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            pass
        # Try extracting first {...} block from mixed output
        m = _re.search(r"\{.*\}", text, _re.DOTALL)
        if m:
            try:
                return _json.loads(m.group(0))
            except _json.JSONDecodeError:
                pass
        return None
