import asyncio
import logging
from collections import OrderedDict
from html import escape
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable

from core.orchestrator import OrchestrationPlan
from core.providers import is_api_provider
from core.task_models import SubtaskRun, TaskResult, TaskRun, utc_now_iso


log = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]

# Keywords that imply a subtask should produce files
_FILE_CREATION_KEYWORDS = frozenset({
    "create", "write", "generate", "implement", "build", "produce",
    "output", "save", "make", "scaffold", "emit",
})


_PROJECT_CONTEXT_FILES = (
    "README.md", "README.rst", "README.txt",
    "pyproject.toml", "setup.py", "Cargo.toml",
    "package.json", "go.mod", "Makefile", "CMakeLists.txt",
)
_SUBTASK_TIMEOUT_SECONDS = 300  # 5 min per subtask


def _cwd_listing(cwd: str, max_entries: int = 30) -> str:
    """Return a compact top-level directory listing for context."""
    try:
        p = Path(cwd)
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        lines = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            lines.append(f"  {entry.name}{'/' if entry.is_dir() else ''}")
            if len(lines) >= max_entries:
                break
        return "\n".join(lines) if lines else ""
    except Exception:
        return ""


def _read_project_context(cwd: str, max_bytes: int = 4000) -> str:
    """Read key project files (README, manifest) for first-subtask context."""
    p = Path(cwd)
    parts: list[str] = []
    budget = max_bytes
    for name in _PROJECT_CONTEXT_FILES:
        candidate = p / name
        if candidate.is_file():
            try:
                text = candidate.read_text(errors="replace")[:budget]
                parts.append(f"--- {name} ---\n{text}")
                budget -= len(text)
                if budget <= 0:
                    break
            except Exception:
                pass
    return "\n\n".join(parts)


def _read_file_contents(file_paths: list[str], max_bytes_each: int = 3000, max_files: int = 4) -> list[tuple[str, str]]:
    """Read contents of key files created by previous subtasks for handoff context."""
    results: list[tuple[str, str]] = []
    # Prefer smaller files and code files
    code_exts = {".py", ".rs", ".ts", ".js", ".go", ".toml", ".json", ".yaml", ".yml", ".md", ".sh"}
    candidates = sorted(
        file_paths,
        key=lambda p: (Path(p).suffix not in code_exts, Path(p).stat().st_size if Path(p).exists() else 0)
    )
    for path_str in candidates[:max_files]:
        p = Path(path_str)
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
            if size > 50_000:
                continue  # skip very large files
            content = p.read_text(errors="replace")[:max_bytes_each]
            results.append((str(p.name), content))
        except Exception:
            pass
    return results


class OrchestratorService:
    def __init__(self, container, execution_service):
        self.container = container
        self.execution_service = execution_service

    # ------------------------------------------------------------------ #
    # Plan building (AI-driven with rule-based fallback)                    #
    # ------------------------------------------------------------------ #

    async def build_plan(
        self,
        session,
        prompt: str,
        stream_event_callback=None,
    ) -> "OrchestrationPlan":
        """Build an orchestration plan.

        Tries AI-driven planning first (if a planning provider is ready),
        then falls back to the rule-based planner.  The result is cached
        inside AIOrchestrator for 1 h so repeated calls for the same prompt
        are instant.
        """
        planning_provider = self.container.pick_planning_provider(session)
        if planning_provider:
            try:
                ai_planner = self.container.build_ai_planner(session)
                await self.container.ensure_runtime_started(session, planning_provider)
                planning_runtime = self.container.get_runtime(session, planning_provider)
                plan = await ai_planner.build_plan(
                    prompt,
                    self.execution_service,
                    session,
                    planning_runtime,
                    stream_event_callback=stream_event_callback,
                )
                return plan
            except Exception as exc:
                log.warning("AI planning failed (%s), falling back to rule-based", exc)
        return self.container.build_planner(session).build_plan(prompt)

    # ------------------------------------------------------------------ #
    # ETA estimation                                                        #
    # ------------------------------------------------------------------ #

    def estimate_plan_eta(self, plan: "OrchestrationPlan", session) -> str:
        """Estimate total execution time using per-provider historical stats."""
        _DEFAULT_MS = 45_000  # 45 s fallback when no history is available

        # Group subtasks by parallel_group; groups run sequentially, tasks within a group run in parallel
        groups: dict[int, list] = {}
        for subtask in plan.subtasks:
            groups.setdefault(subtask.parallel_group, []).append(subtask)

        total_ms = 0
        for group_subtasks in groups.values():
            # For a parallel group the wall-clock time is the max of its members
            group_max = 0
            for subtask in group_subtasks:
                stats = session.provider_stats.get(subtask.suggested_provider)
                avg = stats.avg_ms if (stats and stats.avg_ms > 0) else _DEFAULT_MS
                group_max = max(group_max, avg)
            total_ms += group_max

        # Synthesis step (runs when ≥2 subtasks)
        if len(plan.subtasks) >= 2:
            synth_prov = next(
                (p for p in ("claude", "qwen", "codex") if p in self.container.provider_paths),
                None,
            )
            stats = session.provider_stats.get(synth_prov) if synth_prov else None
            total_ms += stats.avg_ms if (stats and stats.avg_ms > 0) else _DEFAULT_MS

        # Review step (only for complex plans)
        if plan.complexity == "complex":
            total_ms += _DEFAULT_MS // 2

        seconds = total_ms / 1000
        if seconds < 60:
            return f"~{int(seconds)}s"
        return f"~{seconds / 60:.1f}m"

    # ------------------------------------------------------------------ #
    # Subtask result validation                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_subtask_result(subtask, task_result: "TaskResult") -> str | None:
        """Return an error string if the result looks invalid, None if OK.

        Only validates when the subtask description implies file creation/generation.
        """
        if task_result.exit_code != 0:
            return None  # already failed — no additional validation needed

        description_lower = subtask.description.lower()
        expects_files = any(kw in description_lower for kw in _FILE_CREATION_KEYWORDS)

        if expects_files and not task_result.new_files and not task_result.changed_files:
            return (
                "Subtask completed successfully but no files were created or modified. "
                "Please create the required files as described."
            )
        if not task_result.answer_text.strip() and not task_result.new_files:
            return "Subtask produced no output and no files — nothing to hand off."
        return None

    # ------------------------------------------------------------------ #
    # Prompt builders                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def build_subtask_prompt(
        plan: OrchestrationPlan,
        subtask,
        previous_results: list[TaskResult],
        previous_handoffs: list[dict] | None = None,
        cwd: str | None = None,
        files_touched: list[str] | None = None,
        is_first: bool = False,
    ) -> str:
        parts = [
            "You are one agent in a multi-agent execution plan.",
            f"Original task:\n{plan.prompt}",
            (
                f"Your role: {subtask.title}\n"
                f"Task kind: {subtask.task_kind}\n"
                f"Instructions: {subtask.description}"
            ),
        ]
        if cwd:
            listing = _cwd_listing(cwd)
            if listing:
                parts.append(f"Working directory ({Path(cwd).name}):\n{listing}")
        # First subtask: include project manifest/README for context
        if is_first and cwd:
            ctx = _read_project_context(cwd)
            if ctx:
                parts.append(f"Project context:\n{ctx}")
        if files_touched:
            parts.append(
                "Files already created or changed in this run:\n"
                + "\n".join(f"  {f}" for f in files_touched[:20])
            )
            # Include actual file contents for files created by previous subtasks
            file_contents = _read_file_contents(files_touched)
            if file_contents:
                content_parts = ["Key file contents from previous steps:"]
                for fname, content in file_contents:
                    content_parts.append(f"=== {fname} ===\n{content}")
                parts.append("\n\n".join(content_parts))
        if previous_results:
            parts.append("Previous subtask outputs:")
            for result in previous_results[-3:]:
                summary = (
                    f"[{result.provider}] {result.prompt[:150]}\n"
                    f"Result: {result.answer_text[:800]}"
                )
                parts.append(summary)
        if previous_handoffs:
            handoff_lines = ["Structured handoff from previous steps:"]
            for item in previous_handoffs[-4:]:
                if not isinstance(item, dict):
                    continue
                header = (
                    f"- {item.get('title', item.get('subtask_id', 'step'))} "
                    f"[{item.get('provider', 'unknown')}] status={item.get('status', 'unknown')}"
                )
                handoff_lines.append(header)
                summary = str(item.get("summary", "")).strip()
                if summary:
                    handoff_lines.append(f"  summary: {summary[:400]}")
                files = [str(path) for path in item.get("touched_files", []) if str(path).strip()]
                if files:
                    handoff_lines.append("  files: " + ", ".join(Path(path).name for path in files[:8]))
                notes = [str(note) for note in item.get("notes", []) if str(note).strip()]
                if notes:
                    handoff_lines.append("  notes: " + " | ".join(notes[:3]))
            parts.append("\n".join(handoff_lines))
        parts.append(
            "Complete your role thoroughly. Be specific and produce working code or output."
        )
        return "\n\n".join(parts)

    @staticmethod
    def build_handoff_record(subtask_id: str, title: str, task_result: TaskResult) -> dict:
        touched_files = list(dict.fromkeys(task_result.new_files + task_result.changed_files))
        notes: list[str] = []
        if task_result.new_files:
            notes.append(f"created {len(task_result.new_files)} files")
        if task_result.changed_files:
            notes.append(f"changed {len(task_result.changed_files)} files")
        if task_result.error_text.strip():
            notes.append(task_result.error_text[:240])
        return {
            "subtask_id": subtask_id,
            "title": title,
            "provider": task_result.provider,
            "model_name": task_result.model_name,
            "transport": task_result.transport,
            "status": "success" if task_result.exit_code == 0 else "failed",
            "summary": (task_result.answer_text or task_result.error_text or "").strip()[:1200],
            "touched_files": touched_files,
            "new_files": list(task_result.new_files),
            "changed_files": list(task_result.changed_files),
            "notes": notes,
        }

    @staticmethod
    def build_handoff_summary(task_result: TaskResult, title: str) -> str:
        parts = [f"{title} [{task_result.provider}]"]
        if task_result.new_files:
            parts.append("New files: " + ", ".join(Path(p).name for p in task_result.new_files[:8]))
        if task_result.changed_files:
            parts.append("Changed: " + ", ".join(Path(p).name for p in task_result.changed_files[:8]))
        if task_result.answer_text.strip():
            parts.append("Result: " + task_result.answer_text[:800])
        # Include key file contents in handoff so next agent sees actual code
        all_files = task_result.new_files + task_result.changed_files
        if all_files:
            file_contents = _read_file_contents(all_files, max_bytes_each=2000, max_files=3)
            for fname, content in file_contents:
                parts.append(f"\n--- {fname} ---\n{content}")
        return "\n".join(parts)

    @staticmethod
    def build_synthesis_prompt(plan: OrchestrationPlan, task_run: TaskRun) -> str:
        parts = [
            "You are the synthesis step of a multi-agent execution plan.",
            f"Original task:\n{plan.prompt}",
            "Summarize what was implemented, mention key files changed, and call out any remaining issues.",
            "Subtask artifacts:",
        ]
        if task_run.handoff_records:
            for item in task_run.handoff_records[-6:]:
                if not isinstance(item, dict):
                    continue
                line = (
                    f"- {item.get('title', item.get('subtask_id', 'step'))} "
                    f"[{item.get('provider', 'unknown')}] "
                    f"status={item.get('status', 'unknown')}"
                )
                parts.append(line)
                if item.get("summary"):
                    parts.append(f"  summary: {str(item['summary'])[:500]}")
                touched = item.get("touched_files", [])
                if touched:
                    parts.append(
                        "  files: " + ", ".join(Path(str(path)).name for path in touched[:8])
                    )
        for artifact in task_run.handoff_artifacts[-6:]:
            parts.append(f"- {artifact}")
        return "\n\n".join(parts)

    @staticmethod
    def build_review_prompt(plan: OrchestrationPlan, task_run: TaskRun) -> str:
        parts = [
            "You are the final reviewer for a multi-agent execution plan.",
            f"Original task:\n{plan.prompt}",
            "Review the result critically. Keep it concise. Report:",
            "1. Overall verdict",
            "2. Risks or likely gaps",
            "3. Recommended next step",
        ]
        if task_run.answer_text.strip():
            parts.extend(["Final answer:", task_run.answer_text[:4000]])
        if task_run.handoff_artifacts:
            parts.append("Artifacts:")
            for artifact in task_run.handoff_artifacts[-5:]:
                parts.append(f"- {artifact}")
        return "\n\n".join(parts)

    @staticmethod
    def task_result_from_subtask_run(subtask_run: SubtaskRun, prompt: str) -> TaskResult:
        return TaskResult(
            provider=subtask_run.provider,
            prompt=prompt,
            answer_text=subtask_run.answer_text,
            new_files=list(subtask_run.new_files),
            changed_files=list(subtask_run.changed_files),
            exit_code=0 if subtask_run.status in {"success", "reused"} else 1,
            started_at=subtask_run.started_at or utc_now_iso(),
            finished_at=subtask_run.finished_at,
            duration_ms=subtask_run.duration_ms,
            error_text=subtask_run.error_text,
        )

    @staticmethod
    def find_retry_start_index(task_run: TaskRun) -> int | None:
        for index, subtask in enumerate(task_run.subtasks):
            if subtask.status in {"failed", "partial"}:
                return index
        if task_run.mode == "orchestrated" and task_run.status in {"failed", "partial"}:
            return len(task_run.subtasks)
        return None

    @staticmethod
    def validate_plan(plan: OrchestrationPlan) -> str | None:
        """Validate ordered-v1 orchestration semantics.

        For the first public release, depends_on must reference known earlier subtasks.
        """
        seen_ids: list[str] = []
        seen_set: set[str] = set()
        for index, subtask in enumerate(plan.subtasks, start=1):
            if not subtask.subtask_id:
                return f"Subtask #{index} is missing subtask_id."
            if subtask.subtask_id in seen_set:
                return f"Duplicate subtask_id: {subtask.subtask_id}"
            for dep in subtask.depends_on:
                if dep not in seen_set:
                    if dep in [item.subtask_id for item in plan.subtasks[index:]]:
                        return (
                            f"Subtask {subtask.subtask_id} depends on {dep}, "
                            "but ordered-v1 orchestration requires dependencies to point only to earlier steps."
                        )
                    return f"Subtask {subtask.subtask_id} depends on unknown step: {dep}"
            seen_ids.append(subtask.subtask_id)
            seen_set.add(subtask.subtask_id)
        return None

    @staticmethod
    def _completed_subtask_ids(task_run: TaskRun) -> set[str]:
        return {
            item.subtask_id
            for item in task_run.subtasks
            if item.status in {"success", "reused"}
        }

    @staticmethod
    def _scheduled_subtask_ids(task_run: TaskRun) -> set[str]:
        return {item.subtask_id for item in task_run.subtasks}

    @staticmethod
    def _remaining_subtasks(plan: OrchestrationPlan, task_run: TaskRun, resume_from: int = 0) -> list[tuple[int, object]]:
        scheduled_ids = OrchestratorService._scheduled_subtask_ids(task_run)
        return [
            (global_index, subtask)
            for global_index, subtask in enumerate(plan.subtasks, start=1)
            if global_index - 1 >= resume_from and subtask.subtask_id not in scheduled_ids
        ]

    @classmethod
    def _next_ready_group(cls, plan: OrchestrationPlan, task_run: TaskRun, resume_from: int = 0) -> list[tuple[int, object]]:
        remaining = cls._remaining_subtasks(plan, task_run, resume_from=resume_from)
        if not remaining:
            return []
        completed_ids = cls._completed_subtask_ids(task_run)
        ready = [
            (index, subtask)
            for index, subtask in remaining
            if set(subtask.depends_on).issubset(completed_ids)
        ]
        if not ready:
            return []
        first_group = ready[0][1].parallel_group
        return [item for item in ready if item[1].parallel_group == first_group]

    @staticmethod
    def _blocked_subtasks(plan: OrchestrationPlan, task_run: TaskRun, resume_from: int = 0) -> list[tuple[int, object, list[str]]]:
        completed_ids = OrchestratorService._completed_subtask_ids(task_run)
        scheduled_ids = OrchestratorService._scheduled_subtask_ids(task_run)
        blocked: list[tuple[int, object, list[str]]] = []
        for global_index, subtask in enumerate(plan.subtasks, start=1):
            if global_index - 1 < resume_from or subtask.subtask_id in scheduled_ids:
                continue
            missing = [dep for dep in subtask.depends_on if dep not in completed_ids]
            if missing:
                blocked.append((global_index, subtask, missing))
        return blocked

    # ------------------------------------------------------------------ #
    # Dynamic replanning                                                    #
    # ------------------------------------------------------------------ #

    async def _dynamic_replan(
        self,
        session,
        original_plan: "OrchestrationPlan",
        task_run: "TaskRun",
        status_callback: "StatusCallback | None",
        stream_event_callback,
    ) -> list | None:
        """Attempt AI-driven replanning after a partial failure.

        Returns a list of new PlannedSubtask objects, or None if replanning fails.
        """
        from core.orchestrator import AIOrchestrator, RuleBasedOrchestrator

        completed = [s for s in task_run.subtasks if s.status in {"success", "reused"}]
        failed = [s for s in task_run.subtasks if s.status == "failed"]
        if not failed or not completed:
            return None  # nothing to replan or nothing succeeded to build on

        if status_callback:
            await status_callback(
                "⏳ <b>Оркестратор: динамическое перепланирование…</b>"
            )
        log.info("Attempting dynamic replan after %d completed, %d failed", len(completed), len(failed))

        ai_planner = self.container.build_ai_planner(session)
        planning_provider = self.container.pick_planning_provider(session)
        try:
            await self.container.ensure_runtime_started(session, planning_provider)
            planning_runtime = self.container.get_runtime(session, planning_provider)
            new_plan = await ai_planner.replan_remaining(
                original_plan.prompt,
                completed,
                failed[-1],
                self.execution_service,
                session,
                planning_runtime,
            )
            if new_plan is not None and new_plan.subtasks:
                log.info("Dynamic replan produced %d new subtasks", len(new_plan.subtasks))
                return new_plan.subtasks
        except Exception as exc:
            log.warning("Dynamic replan failed: %s", exc)
        return None

    # ------------------------------------------------------------------ #
    # Health-aware provider routing                                         #
    # ------------------------------------------------------------------ #

    def _is_provider_available(self, session, name: str) -> bool:
        """Check if a provider is currently available (respects retry_after_ts)."""
        runtime = session.runtimes.get(name)
        if runtime is None or runtime.health is None:
            return True  # no health info → assume available
        return runtime.health.is_available_now()

    def _pick_healthy_provider(self, session, preferred: str, allow_api: bool = True) -> str:
        """Return preferred provider if healthy, else first available alternative."""
        if self._is_provider_available(session, preferred):
            return preferred
        # Preferred is blocked — find an alternative
        for name in self.container.provider_paths:
            if name == preferred:
                continue
            if not allow_api and is_api_provider(name):
                continue
            if self._is_provider_available(session, name):
                runtime = session.runtimes.get(preferred)
                eta = runtime.health.retry_in_seconds if (runtime and runtime.health) else None
                log.info(
                    "Provider %s unavailable (retry in %ss), routing to %s",
                    preferred, eta, name,
                )
                return name
        return preferred  # no healthy alternative found, use preferred anyway

    def _find_alt_provider(self, session, used: str, allow_api: bool = True) -> str | None:
        """Find an alternative provider different from `used`."""
        for name in self.container.provider_paths:
            if name == used:
                continue
            if not allow_api and is_api_provider(name):
                continue
            if self._is_provider_available(session, name):
                return name
        return None

    # ------------------------------------------------------------------ #
    # Core execution                                                        #
    # ------------------------------------------------------------------ #

    async def _execute_subtask(
        self,
        session,
        plan: OrchestrationPlan,
        subtask,
        provider_name: str,
        prompt: str,
        status_callback: StatusCallback | None,
        status_prefix: str,
        stream_event_callback,
        interaction_callback=None,
    ) -> TaskResult:
        await self.container.ensure_runtime_started(session, provider_name)
        runtime = self.container.get_runtime(session, provider_name)
        session.active_provider = provider_name
        return await self.execution_service.execute_provider_task(
            session=session,
            runtime=runtime,
            provider_name=provider_name,
            prompt=prompt,
            status_callback=status_callback,
            status_prefix=status_prefix,
            stream_event_callback=stream_event_callback,
            interaction_callback=interaction_callback,
        )

    async def _execute_subtask_timed(
        self,
        session,
        plan: OrchestrationPlan,
        subtask,
        provider_name: str,
        prompt: str,
        status_callback: StatusCallback | None,
        status_prefix: str,
        stream_event_callback,
        interaction_callback=None,
    ) -> TaskResult:
        """Like _execute_subtask but enforces _SUBTASK_TIMEOUT_SECONDS."""
        try:
            return await asyncio.wait_for(
                self._execute_subtask(
                    session, plan, subtask, provider_name, prompt,
                    status_callback, status_prefix, stream_event_callback,
                    interaction_callback=interaction_callback,
                ),
                timeout=_SUBTASK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("Subtask %s timed out after %ds", subtask.subtask_id, _SUBTASK_TIMEOUT_SECONDS)
            from core.task_models import TaskResult, utc_now_iso
            return TaskResult(
                provider=provider_name,
                prompt=prompt,
                exit_code=-1,
                error_text=f"Subtask timed out after {_SUBTASK_TIMEOUT_SECONDS}s",
                started_at=utc_now_iso(),
                finished_at=utc_now_iso(),
            )

    async def run_orchestrated_task(
        self,
        session,
        plan: OrchestrationPlan,
        status_callback: StatusCallback | None = None,
        resume_from: int = 0,
        prior_subtasks: list[SubtaskRun] | None = None,
        stream_event_callback=None,
        interaction_callback=None,
    ) -> tuple[TaskRun, TaskResult]:
        started_at = monotonic()
        subtask_results: list[TaskResult] = []
        prior_subtasks = list(prior_subtasks or [])
        plan_error = self.validate_plan(plan)

        task_run = TaskRun(
            run_id=f"run-{utc_now_iso()}",
            prompt=plan.prompt,
            mode="orchestrated",
            status="running",
            strategy=plan.strategy,
            complexity=plan.complexity,
            provider_summary=" -> ".join(s.suggested_provider for s in plan.subtasks),
            started_at=utc_now_iso(),
            ai_plan_rationale=plan.ai_rationale,
        )

        cwd = str(session.file_mgr.get_working_dir())

        if plan_error:
            task_run.status = "failed"
            task_run.finished_at = utc_now_iso()
            task_run.duration_ms = int((monotonic() - started_at) * 1000)
            task_run.error_text = plan_error
            self.container.metrics.record_orchestrated_run(task_run.status)
            aggregate_result = TaskResult(
                provider=session.current_provider,
                prompt=plan.prompt,
                answer_text="",
                exit_code=1,
                started_at=task_run.started_at,
                finished_at=task_run.finished_at,
                duration_ms=task_run.duration_ms,
                error_text=plan_error,
            )
            session.last_task_run = task_run
            session.last_task_result = aggregate_result
            session.run_history.append(task_run)
            if len(session.run_history) > 10:
                session.run_history = session.run_history[-10:]
            session.history.append(aggregate_result)
            if len(session.history) > 10:
                session.history = session.history[-10:]
            task_run.artifact_file = self.container.session_store.write_run_artifact(session, task_run)
            self.container.save_session(session)
            return task_run, aggregate_result

        # Restore reused subtasks from a previous (partial) run
        for preserved in prior_subtasks[:resume_from]:
            reused = SubtaskRun(
                subtask_id=preserved.subtask_id,
                title=preserved.title,
                provider=preserved.provider,
                model_name=preserved.model_name,
                transport=preserved.transport,
                task_kind=preserved.task_kind,
                description=preserved.description,
                depends_on=list(preserved.depends_on),
                status="reused",
                answer_text=preserved.answer_text,
                error_text=preserved.error_text,
                started_at=preserved.started_at,
                finished_at=preserved.finished_at,
                duration_ms=preserved.duration_ms,
                input_tokens=preserved.input_tokens,
                output_tokens=preserved.output_tokens,
                new_files=list(preserved.new_files),
                changed_files=list(preserved.changed_files),
                handoff_summary=preserved.handoff_summary,
                handoff_record=dict(preserved.handoff_record),
            )
            task_run.subtasks.append(reused)
            if reused.handoff_summary:
                task_run.handoff_artifacts.append(reused.handoff_summary)
            if reused.handoff_record:
                task_run.handoff_records.append(dict(reused.handoff_record))
            subtask_results.append(self.task_result_from_subtask_run(reused, plan.prompt))

        replan_attempted = False
        while True:
            group = self._next_ready_group(plan, task_run, resume_from=resume_from)
            if not group:
                break
            if len(group) == 1:
                ok = await self._run_group_sequential(
                    session, plan, task_run, subtask_results,
                    group, cwd, status_callback, stream_event_callback,
                )
            else:
                ok = await self._run_group_parallel(
                    session, plan, task_run, subtask_results,
                    group, cwd, status_callback, stream_event_callback,
                )

            if not ok and not replan_attempted:
                replan_attempted = True
                # Attempt dynamic replanning — only when some subtasks already succeeded
                new_subtasks = await self._dynamic_replan(
                    session, plan, task_run, status_callback, stream_event_callback
                )
                if new_subtasks:
                    # Run the new subtasks sequentially
                    base_index = len(task_run.subtasks) + 1
                    all_new_ok = True
                    for offset, new_subtask in enumerate(new_subtasks):
                        new_ok = await self._run_group_sequential(
                            session, plan, task_run, subtask_results,
                            [(base_index + offset, new_subtask)],
                            cwd, status_callback, stream_event_callback,
                        )
                        if not new_ok:
                            all_new_ok = False
                            break
                    if all_new_ok:
                        task_run.status = "running"
                        task_run.error_text = ""
                        continue
            # Continue scheduling other ready groups even after a failure.

        blocked_subtasks = self._blocked_subtasks(plan, task_run, resume_from=resume_from)
        for index, subtask, missing in blocked_subtasks:
            if any(item.subtask_id == subtask.subtask_id for item in task_run.subtasks):
                continue
            message = (
                "Skipped because dependencies were not completed: "
                + ", ".join(missing)
            )
            task_run.subtasks.append(
                SubtaskRun(
                    subtask_id=subtask.subtask_id,
                    title=subtask.title,
                    provider=subtask.suggested_provider,
                    task_kind=subtask.task_kind,
                    description=subtask.description,
                    depends_on=list(subtask.depends_on),
                    status="skipped",
                    error_text=message,
                )
            )
            if task_run.status == "running":
                task_run.status = "partial"
            if not task_run.error_text:
                task_run.error_text = message

        session.active_provider = ""
        task_run.duration_ms = int((monotonic() - started_at) * 1000)
        task_run.finished_at = utc_now_iso()
        if task_run.status == "running":
            task_run.status = "success"

        # ---- Synthesis: only when 2+ subtasks succeeded ----
        successful = [s for s in task_run.subtasks if s.status in {"success", "reused"}]
        if len(successful) >= 2:
            await self._run_synthesis(
                session, plan, task_run, status_callback, stream_event_callback
            )
        elif successful:
            # Single subtask — just use its answer directly
            task_run.answer_text = successful[0].answer_text

        # ---- Review: only for complex tasks ----
        if plan.complexity == "complex" and task_run.answer_text.strip():
            await self._run_review(
                session, plan, task_run, status_callback, stream_event_callback
            )

        provider_parts = [item.provider for item in task_run.subtasks if item.provider]
        model_parts = [item.model_name for item in task_run.subtasks if item.model_name]
        transport_parts = [item.transport for item in task_run.subtasks if item.transport]
        if task_run.synthesis_provider:
            provider_parts.append(task_run.synthesis_provider)
        if task_run.synthesis_model:
            model_parts.append(task_run.synthesis_model)
        if task_run.synthesis_transport:
            transport_parts.append(task_run.synthesis_transport)
        if task_run.review_provider:
            provider_parts.append(task_run.review_provider)
        if task_run.review_model:
            model_parts.append(task_run.review_model)
        if task_run.review_transport:
            transport_parts.append(task_run.review_transport)
        task_run.provider_summary = " -> ".join(dict.fromkeys(provider_parts)) or task_run.provider_summary
        task_run.model_summary = " -> ".join(dict.fromkeys(model_parts))
        task_run.transport_summary = " -> ".join(dict.fromkeys(transport_parts))

        # Clear checkpoint on success; keep it on failure for /recover
        if task_run.status == "success":
            self.container.session_store.clear_checkpoint(session.chat_id)

        # Persist
        self.container.metrics.record_orchestrated_run(task_run.status)
        session.last_task_run = task_run
        session.run_history.append(task_run)
        if len(session.run_history) > 10:
            session.run_history = session.run_history[-10:]

        last_provider = task_run.subtasks[-1].provider if task_run.subtasks else session.current_provider
        aggregate_result = TaskResult(
            provider=last_provider,
            model_name=task_run.synthesis_model or (task_run.subtasks[-1].model_name if task_run.subtasks else ""),
            transport=task_run.synthesis_transport or (task_run.subtasks[-1].transport if task_run.subtasks else "cli"),
            prompt=plan.prompt,
            answer_text=task_run.answer_text,
            new_files=task_run.new_files,
            changed_files=task_run.changed_files,
            input_tokens=task_run.input_tokens,
            output_tokens=task_run.output_tokens,
            total_input_tokens=task_run.total_input_tokens,
            total_output_tokens=task_run.total_output_tokens,
            exit_code=0 if task_run.status == "success" else 1,
            started_at=task_run.started_at,
            finished_at=task_run.finished_at,
            duration_ms=task_run.duration_ms,
            error_text=task_run.error_text,
        )
        session.last_task_result = aggregate_result
        session.history.append(aggregate_result)
        if len(session.history) > 10:
            session.history = session.history[-10:]
        task_run.artifact_file = self.container.session_store.write_run_artifact(session, task_run)
        self.container.save_session(session)
        return task_run, aggregate_result

    # ------------------------------------------------------------------ #
    # Group runners                                                         #
    # ------------------------------------------------------------------ #

    async def _run_group_sequential(
        self, session, plan, task_run, subtask_results,
        group, cwd, status_callback, stream_event_callback,
    ) -> bool:
        """Run a single-item group (or force-sequential). Returns False on hard failure."""
        index, subtask = group[0]
        ok = await self._run_one_subtask(
            session, plan, task_run, subtask_results,
            index, subtask, cwd, status_callback, stream_event_callback,
        )
        return ok

    async def _run_group_parallel(
        self, session, plan, task_run, subtask_results,
        group, cwd, status_callback, stream_event_callback,
    ) -> bool:
        """Run a parallel group via asyncio.gather. Returns False if any subtask hard-fails."""
        # We need separate result lists per subtask then merge in order
        per_subtask_results: list[list[TaskResult]] = [
            list(subtask_results) for _ in group
        ]

        async def run_one(pos: int, index: int, subtask) -> bool:
            return await self._run_one_subtask(
                session, plan, task_run, per_subtask_results[pos],
                index, subtask, cwd, status_callback, stream_event_callback,
            )

        results = await asyncio.gather(
            *[run_one(pos, index, subtask) for pos, (index, subtask) in enumerate(group)],
            return_exceptions=False,
        )
        # Merge results back: add any new TaskResults from parallel runs to subtask_results
        all_new: list[TaskResult] = []
        for per in per_subtask_results:
            for r in per[len(subtask_results):]:
                if r not in all_new:
                    all_new.append(r)
        subtask_results.extend(all_new)
        return all(results)

    async def _run_one_subtask(
        self,
        session,
        plan: OrchestrationPlan,
        task_run: TaskRun,
        subtask_results: list[TaskResult],
        index: int,
        subtask,
        cwd: str,
        status_callback: StatusCallback | None,
        stream_event_callback,
    ) -> bool:
        """Execute one subtask with health-aware routing, per-subtask timeout, and one retry."""
        provider_name = self._pick_healthy_provider(session, subtask.suggested_provider, allow_api=False)
        is_first = index == 1 and not subtask_results

        # Collect already-touched files for context
        files_touched = list(dict.fromkeys(
            f for r in subtask_results for f in (r.new_files + r.changed_files)
        ))

        prompt = self.build_subtask_prompt(
            plan, subtask, subtask_results,
            previous_handoffs=task_run.handoff_records,
            cwd=cwd, files_touched=files_touched, is_first=is_first,
        )

        status_title = "⏳ <b>Оркестратор выполняет план</b>"
        status_prefix = (
            f"{status_title}\n\n"
            f"<b>Шаг {index}/{len(plan.subtasks)}</b>: {escape(subtask.title)}\n"
            f"<b>Агент:</b> <code>{escape(provider_name)}</code>"
        )
        if status_callback:
            await status_callback(status_prefix)

        task_result = await self._execute_subtask_timed(
            session, plan, subtask, provider_name, prompt,
            status_callback, status_prefix, stream_event_callback,
        )

        retry_count = 0
        original_provider = ""

        # Retry with alternative provider on failure
        if task_result.exit_code != 0:
            alt = self._find_alt_provider(session, provider_name, allow_api=False)
            if alt:
                original_provider = provider_name
                retry_count = 1
                log.info("Subtask %s failed on %s, retrying with %s", subtask.subtask_id, provider_name, alt)
                retry_prefix = (
                    f"{status_title}\n\n"
                    f"<b>Шаг {index}/{len(plan.subtasks)} (retry → {escape(alt)})</b>: "
                    f"{escape(subtask.title)}"
                )
                if status_callback:
                    await status_callback(retry_prefix)
                task_result = await self._execute_subtask_timed(
                    session, plan, subtask, alt, prompt,
                    status_callback, retry_prefix, stream_event_callback,
                )
                provider_name = alt

        # Validate the result if it passed — retry once with an augmented prompt if invalid
        if task_result.exit_code == 0:
            validation_error = self._validate_subtask_result(subtask, task_result)
            if validation_error and not task_result.new_files and not task_result.changed_files:
                log.info(
                    "Subtask %s validation failed (%s), retrying with augmented prompt",
                    subtask.subtask_id, validation_error,
                )
                augmented = (
                    prompt
                    + f"\n\nIMPORTANT: {validation_error}"
                )
                retry_result = await self._execute_subtask_timed(
                    session, plan, subtask, provider_name, augmented,
                    status_callback, status_prefix, stream_event_callback,
                )
                if retry_result.exit_code == 0:
                    task_result = retry_result

        subtask_results.append(task_result)
        handoff_record = self.build_handoff_record(subtask.subtask_id, subtask.title, task_result)
        handoff_summary = self.build_handoff_summary(task_result, subtask.title)
        task_run.handoff_artifacts.append(handoff_summary)
        task_run.handoff_records.append(handoff_record)

        sub_status = "success" if task_result.exit_code == 0 else "failed"
        task_run.subtasks.append(SubtaskRun(
            subtask_id=subtask.subtask_id,
            title=subtask.title,
            provider=provider_name,
            model_name=task_result.model_name,
            transport=task_result.transport,
            task_kind=subtask.task_kind,
            description=subtask.description,
            depends_on=list(subtask.depends_on),
            status=sub_status,
            answer_text=task_result.answer_text,
            error_text=task_result.error_text,
            started_at=task_result.started_at,
            finished_at=task_result.finished_at,
            duration_ms=task_result.duration_ms,
            input_tokens=task_result.input_tokens,
            output_tokens=task_result.output_tokens,
            new_files=list(task_result.new_files),
            changed_files=list(task_result.changed_files),
            handoff_summary=handoff_summary,
            handoff_record=handoff_record,
            retry_count=retry_count,
            original_provider=original_provider,
        ))
        task_run.input_tokens += task_result.input_tokens
        task_run.output_tokens += task_result.output_tokens
        task_run.total_input_tokens += task_result.total_input_tokens
        task_run.total_output_tokens += task_result.total_output_tokens

        # Persist checkpoint after every subtask so crashes can be recovered
        self.container.session_store.write_checkpoint(session, task_run)

        if task_result.exit_code != 0:
            if index == 1 and len(plan.subtasks) == 1:
                task_run.status = "failed"
            else:
                task_run.status = "partial"
            task_run.error_text = task_result.error_text or f"Subtask {subtask.subtask_id} failed"
            return False

        return True

    # ------------------------------------------------------------------ #
    # Synthesis and review                                                  #
    # ------------------------------------------------------------------ #

    async def _run_synthesis(
        self, session, plan, task_run, status_callback, stream_event_callback
    ):
        synthesis_provider = self._pick_synthesis_provider(session)
        task_run.synthesis_provider = synthesis_provider
        await self.container.ensure_runtime_started(session, synthesis_provider)
        synthesis_runtime = self.container.get_runtime(session, synthesis_provider)
        session.active_provider = synthesis_provider
        task_run.synthesis_prompt = self.build_synthesis_prompt(plan, task_run)
        prefix = (
            "⏳ <b>Оркестратор собирает итог</b>\n\n"
            f"<b>Синтезатор:</b> <code>{escape(synthesis_provider)}</code>"
        )
        if status_callback:
            await status_callback(prefix)
        result = await self.execution_service.execute_provider_task(
            session=session,
            runtime=synthesis_runtime,
            provider_name=synthesis_provider,
            prompt=task_run.synthesis_prompt,
            status_callback=status_callback,
            status_prefix=prefix,
            stream_event_callback=stream_event_callback,
        )
        task_run.synthesis_model = result.model_name
        task_run.synthesis_transport = result.transport
        task_run.total_input_tokens += result.total_input_tokens
        task_run.total_output_tokens += result.total_output_tokens
        if result.exit_code == 0 and result.answer_text.strip():
            task_run.synthesis_answer = result.answer_text
            task_run.answer_text = result.answer_text
            task_run.handoff_artifacts.append(self.build_handoff_summary(result, "Synthesis"))
        else:
            task_run.answer_text = "\n\n".join(
                f"[{s.provider}] {s.title}\n{s.answer_text}".strip()
                for s in task_run.subtasks
                if s.answer_text.strip()
            )
            if task_run.status == "success":
                task_run.status = "partial"
            if not task_run.error_text:
                task_run.error_text = result.error_text or "Synthesis failed"

    async def _run_review(
        self, session, plan, task_run, status_callback, stream_event_callback
    ):
        review_provider = self._pick_review_provider(session)
        task_run.review_provider = review_provider
        await self.container.ensure_runtime_started(session, review_provider)
        review_runtime = self.container.get_runtime(session, review_provider)
        session.active_provider = review_provider
        task_run.review_prompt = self.build_review_prompt(plan, task_run)
        prefix = (
            "⏳ <b>Оркестратор выполняет review</b>\n\n"
            f"<b>Reviewer:</b> <code>{escape(review_provider)}</code>"
        )
        if status_callback:
            await status_callback(prefix)
        result = await self.execution_service.execute_provider_task(
            session=session,
            runtime=review_runtime,
            provider_name=review_provider,
            prompt=task_run.review_prompt,
            status_callback=status_callback,
            status_prefix=prefix,
            stream_event_callback=stream_event_callback,
        )
        task_run.review_model = result.model_name
        task_run.review_transport = result.transport
        task_run.total_input_tokens += result.total_input_tokens
        task_run.total_output_tokens += result.total_output_tokens
        if result.exit_code == 0 and result.answer_text.strip():
            task_run.review_answer = result.answer_text
            task_run.handoff_artifacts.append(self.build_handoff_summary(result, "Review"))
        elif task_run.status == "success":
            task_run.status = "partial"

    # ------------------------------------------------------------------ #
    # Provider selection helpers                                            #
    # ------------------------------------------------------------------ #

    def _pick_synthesis_provider(self, session) -> str:
        for preferred in ("openrouter", "claude", "qwen", "codex"):
            if preferred in self.container.provider_paths and self.container.provider_is_ready(preferred)[0]:
                return preferred
        subtasks = getattr(session, "last_task_run", None)
        if subtasks and subtasks.subtasks:
            return subtasks.subtasks[-1].provider
        return session.current_provider

    def _pick_review_provider(self, session) -> str:
        for preferred in ("openrouter", "codex", "claude", "qwen"):
            if preferred in self.container.provider_paths and self.container.provider_is_ready(preferred)[0]:
                return preferred
        return session.current_provider
