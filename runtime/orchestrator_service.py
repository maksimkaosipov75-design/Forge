import asyncio
import logging
from html import escape
from itertools import groupby
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable

from orchestrator import OrchestrationPlan
from task_models import SubtaskRun, TaskResult, TaskRun, utc_now_iso


log = logging.getLogger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]


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


class OrchestratorService:
    def __init__(self, container, execution_service):
        self.container = container
        self.execution_service = execution_service

    # ------------------------------------------------------------------ #
    # Prompt builders                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def build_subtask_prompt(
        plan: OrchestrationPlan,
        subtask,
        previous_results: list[TaskResult],
        cwd: str | None = None,
        files_touched: list[str] | None = None,
    ) -> str:
        parts = [
            "You are one agent in a multi-agent execution plan.",
            f"Overall task:\n{plan.prompt}",
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
        if files_touched:
            parts.append(
                "Files already created or changed in this run:\n"
                + "\n".join(f"  {f}" for f in files_touched[:20])
            )
        if subtask.depends_on and previous_results:
            parts.append("Previous subtask outputs (context for your work):")
            for result in previous_results[-3:]:
                summary = (
                    f"[{result.provider}] {result.prompt[:200]}\n"
                    f"Result: {result.answer_text[:1200]}"
                )
                parts.append(summary)
        parts.append(
            "Complete your role thoroughly. Be specific and produce working code or output."
        )
        return "\n\n".join(parts)

    @staticmethod
    def build_handoff_summary(task_result: TaskResult, title: str) -> str:
        parts = [f"{title} [{task_result.provider}]"]
        if task_result.new_files:
            parts.append("New files: " + ", ".join(Path(p).name for p in task_result.new_files[:8]))
        if task_result.changed_files:
            parts.append("Changed: " + ", ".join(Path(p).name for p in task_result.changed_files[:8]))
        if task_result.answer_text.strip():
            parts.append("Result: " + task_result.answer_text[:800])
        return "\n".join(parts)

    @staticmethod
    def build_synthesis_prompt(plan: OrchestrationPlan, task_run: TaskRun) -> str:
        parts = [
            "You are the synthesis step of a multi-agent execution plan.",
            f"Original task:\n{plan.prompt}",
            "Summarize what was implemented, mention key files changed, and call out any remaining issues.",
            "Subtask artifacts:",
        ]
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

    # ------------------------------------------------------------------ #
    # Health-aware provider routing                                         #
    # ------------------------------------------------------------------ #

    def _pick_healthy_provider(self, session, preferred: str) -> str:
        """Return preferred provider if healthy, else first healthy alternative."""
        runtime = session.runtimes.get(preferred)
        if runtime is None or runtime.health is None or runtime.health.available:
            return preferred
        # Preferred is unhealthy — find an alternative
        for name in self.container.provider_paths:
            if name == preferred:
                continue
            alt = session.runtimes.get(name)
            if alt is None or alt.health is None or alt.health.available:
                log.info("Provider %s unhealthy, routing to %s", preferred, name)
                return name
        return preferred  # no healthy alternative found, use preferred anyway

    def _find_alt_provider(self, session, used: str) -> str | None:
        """Find an alternative provider different from `used`."""
        for name in self.container.provider_paths:
            if name == used:
                continue
            alt = session.runtimes.get(name)
            if alt is None or alt.health is None or alt.health.available:
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
        )

    async def run_orchestrated_task(
        self,
        session,
        plan: OrchestrationPlan,
        status_callback: StatusCallback | None = None,
        resume_from: int = 0,
        prior_subtasks: list[SubtaskRun] | None = None,
        stream_event_callback=None,
    ) -> tuple[TaskRun, TaskResult]:
        started_at = monotonic()
        subtask_results: list[TaskResult] = []
        prior_subtasks = list(prior_subtasks or [])

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

        # Restore reused subtasks from a previous (partial) run
        for preserved in prior_subtasks[:resume_from]:
            reused = SubtaskRun(
                subtask_id=preserved.subtask_id,
                title=preserved.title,
                provider=preserved.provider,
                task_kind=preserved.task_kind,
                description=preserved.description,
                depends_on=list(preserved.depends_on),
                status="reused",
                answer_text=preserved.answer_text,
                error_text=preserved.error_text,
                started_at=preserved.started_at,
                finished_at=preserved.finished_at,
                duration_ms=preserved.duration_ms,
                new_files=list(preserved.new_files),
                changed_files=list(preserved.changed_files),
                handoff_summary=preserved.handoff_summary,
            )
            task_run.subtasks.append(reused)
            if reused.handoff_summary:
                task_run.handoff_artifacts.append(reused.handoff_summary)
            subtask_results.append(self.task_result_from_subtask_run(reused, plan.prompt))

        # Group subtasks by parallel_group for concurrent execution
        pending = [
            (global_index, subtask)
            for global_index, subtask in enumerate(plan.subtasks, start=1)
            if global_index - 1 >= resume_from
        ]
        groups: list[list[tuple[int, object]]] = []
        for _pg, grp in groupby(pending, key=lambda x: x[1].parallel_group):
            groups.append(list(grp))

        for group in groups:
            if task_run.status in {"failed"}:
                break  # hard failure in previous group — stop

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
            if not ok:
                break  # group had a hard failure

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

        # Persist
        session.last_task_run = task_run
        session.run_history.append(task_run)
        if len(session.run_history) > 10:
            session.run_history = session.run_history[-10:]

        last_provider = task_run.subtasks[-1].provider if task_run.subtasks else session.current_provider
        aggregate_result = TaskResult(
            provider=last_provider,
            prompt=plan.prompt,
            answer_text=task_run.answer_text,
            new_files=task_run.new_files,
            changed_files=task_run.changed_files,
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
        """Execute one subtask with health-aware routing and one retry. Returns True on success."""
        provider_name = self._pick_healthy_provider(session, subtask.suggested_provider)

        # Collect already-touched files for context
        files_touched = list(dict.fromkeys(
            f for r in subtask_results for f in (r.new_files + r.changed_files)
        ))

        prompt = self.build_subtask_prompt(
            plan, subtask, subtask_results, cwd=cwd, files_touched=files_touched
        )

        status_title = "⏳ <b>Оркестратор выполняет план</b>"
        status_prefix = (
            f"{status_title}\n\n"
            f"<b>Шаг {index}/{len(plan.subtasks)}</b>: {escape(subtask.title)}\n"
            f"<b>Агент:</b> <code>{escape(provider_name)}</code>"
        )
        if status_callback:
            await status_callback(status_prefix)

        task_result = await self._execute_subtask(
            session, plan, subtask, provider_name, prompt,
            status_callback, status_prefix, stream_event_callback,
        )

        retry_count = 0
        original_provider = ""

        # Retry with alternative provider on failure
        if task_result.exit_code != 0:
            alt = self._find_alt_provider(session, provider_name)
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
                task_result = await self._execute_subtask(
                    session, plan, subtask, alt, prompt,
                    status_callback, retry_prefix, stream_event_callback,
                )
                provider_name = alt

        subtask_results.append(task_result)
        handoff_summary = self.build_handoff_summary(task_result, subtask.title)
        task_run.handoff_artifacts.append(handoff_summary)

        sub_status = "success" if task_result.exit_code == 0 else "failed"
        task_run.subtasks.append(SubtaskRun(
            subtask_id=subtask.subtask_id,
            title=subtask.title,
            provider=provider_name,
            task_kind=subtask.task_kind,
            description=subtask.description,
            depends_on=list(subtask.depends_on),
            status=sub_status,
            answer_text=task_result.answer_text,
            error_text=task_result.error_text,
            started_at=task_result.started_at,
            finished_at=task_result.finished_at,
            duration_ms=task_result.duration_ms,
            new_files=list(task_result.new_files),
            changed_files=list(task_result.changed_files),
            handoff_summary=handoff_summary,
            retry_count=retry_count,
            original_provider=original_provider,
        ))

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
        if result.exit_code == 0 and result.answer_text.strip():
            task_run.review_answer = result.answer_text
            task_run.handoff_artifacts.append(self.build_handoff_summary(result, "Review"))
        elif task_run.status == "success":
            task_run.status = "partial"

    # ------------------------------------------------------------------ #
    # Provider selection helpers                                            #
    # ------------------------------------------------------------------ #

    def _pick_synthesis_provider(self, session) -> str:
        for preferred in ("claude", "qwen", "codex"):
            if preferred in self.container.provider_paths:
                return preferred
        subtasks = getattr(session, "last_task_run", None)
        if subtasks and subtasks.subtasks:
            return subtasks.subtasks[-1].provider
        return session.current_provider

    def _pick_review_provider(self, session) -> str:
        for preferred in ("codex", "claude", "qwen"):
            if preferred in self.container.provider_paths:
                return preferred
        return session.current_provider
