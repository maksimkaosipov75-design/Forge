from html import escape
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable

from orchestrator import OrchestrationPlan
from task_models import SubtaskRun, TaskResult, TaskRun, utc_now_iso


StatusCallback = Callable[[str], Awaitable[None]]


class OrchestratorService:
    def __init__(self, container, execution_service):
        self.container = container
        self.execution_service = execution_service

    @staticmethod
    def build_subtask_prompt(plan: OrchestrationPlan, subtask, previous_results: list[TaskResult]) -> str:
        parts = [
            "You are working as part of a multi-agent execution plan.",
            f"Original task:\n{plan.prompt}",
            (
                f"Current subtask: {subtask.title}\n"
                f"Task kind: {subtask.task_kind}\n"
                f"Description: {subtask.description}"
            ),
        ]
        if subtask.depends_on and previous_results:
            parts.append("Previous subtask outputs:")
            for result in previous_results[-3:]:
                parts.append(
                    f"- Provider: {result.provider}\n"
                    f"  Prompt: {result.prompt[:300]}\n"
                    f"  Result: {result.answer_text[:1200]}"
                )
        parts.append("Return the implementation result for this subtask, keeping the response concise but actionable.")
        return "\n\n".join(parts)

    @staticmethod
    def build_handoff_summary(task_result: TaskResult, title: str) -> str:
        parts = [f"{title} [{task_result.provider}]"]
        if task_result.new_files:
            parts.append("New files: " + ", ".join(Path(path).name for path in task_result.new_files[:8]))
        if task_result.changed_files:
            parts.append("Changed files: " + ", ".join(Path(path).name for path in task_result.changed_files[:8]))
        if task_result.answer_text.strip():
            parts.append("Result: " + task_result.answer_text[:700])
        return "\n".join(parts)

    @staticmethod
    def build_synthesis_prompt(plan: OrchestrationPlan, task_run: TaskRun) -> str:
        parts = [
            "You are the synthesis step of a multi-agent execution plan.",
            f"Original task:\n{plan.prompt}",
            "Summarize the work completed by the subtasks, explain what was implemented, mention key files changed, and call out any remaining issues.",
            "Subtask handoff artifacts:",
        ]
        for artifact in task_run.handoff_artifacts[-6:]:
            parts.append(f"- {artifact}")
        return "\n\n".join(parts)

    @staticmethod
    def build_review_prompt(plan: OrchestrationPlan, task_run: TaskRun) -> str:
        parts = [
            "You are the final reviewer for a multi-agent execution plan.",
            f"Original task:\n{plan.prompt}",
            "Review the final result critically. Keep it concise. Report:",
            "1. Overall verdict",
            "2. Risks or likely gaps",
            "3. Recommended next step",
        ]
        if task_run.answer_text.strip():
            parts.extend(["Final synthesized answer:", task_run.answer_text[:5000]])
        if task_run.handoff_artifacts:
            parts.append("Handoff artifacts:")
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
            provider_summary=" -> ".join(subtask.suggested_provider for subtask in plan.subtasks),
            started_at=utc_now_iso(),
        )

        for preserved_subtask in prior_subtasks[:resume_from]:
            reused_subtask = SubtaskRun(
                subtask_id=preserved_subtask.subtask_id,
                title=preserved_subtask.title,
                provider=preserved_subtask.provider,
                task_kind=preserved_subtask.task_kind,
                description=preserved_subtask.description,
                depends_on=list(preserved_subtask.depends_on),
                status="reused",
                answer_text=preserved_subtask.answer_text,
                error_text=preserved_subtask.error_text,
                started_at=preserved_subtask.started_at,
                finished_at=preserved_subtask.finished_at,
                duration_ms=preserved_subtask.duration_ms,
                new_files=list(preserved_subtask.new_files),
                changed_files=list(preserved_subtask.changed_files),
                handoff_summary=preserved_subtask.handoff_summary,
            )
            task_run.subtasks.append(reused_subtask)
            if reused_subtask.handoff_summary:
                task_run.handoff_artifacts.append(reused_subtask.handoff_summary)
            subtask_results.append(self.task_result_from_subtask_run(reused_subtask, plan.prompt))

        for index, subtask in enumerate(plan.subtasks, start=1):
            if index - 1 < resume_from:
                continue
            provider_name = subtask.suggested_provider
            await self.container.ensure_runtime_started(session, provider_name)
            runtime = self.container.get_runtime(session, provider_name)
            session.active_provider = provider_name
            status_title = "⏳ <b>Оркестратор возобновляет план</b>" if resume_from else "⏳ <b>Оркестратор выполняет план</b>"
            status_prefix = (
                f"{status_title}\n\n"
                f"<b>Шаг {index}/{len(plan.subtasks)}</b>: {escape(subtask.title)}\n"
                f"<b>Агент:</b> <code>{escape(provider_name)}</code>"
            )
            if status_callback:
                await status_callback(status_prefix)
            task_result = await self.execution_service.execute_provider_task(
                session=session,
                runtime=runtime,
                provider_name=provider_name,
                prompt=self.build_subtask_prompt(plan, subtask, subtask_results),
                status_callback=status_callback,
                status_prefix=status_prefix,
                stream_event_callback=stream_event_callback,
            )
            subtask_results.append(task_result)
            handoff_summary = self.build_handoff_summary(task_result, subtask.title)
            task_run.handoff_artifacts.append(handoff_summary)
            task_run.subtasks.append(
                SubtaskRun(
                    subtask_id=subtask.subtask_id,
                    title=subtask.title,
                    provider=provider_name,
                    task_kind=subtask.task_kind,
                    description=subtask.description,
                    depends_on=list(subtask.depends_on),
                    status="success" if task_result.exit_code == 0 else "failed",
                    answer_text=task_result.answer_text,
                    error_text=task_result.error_text,
                    started_at=task_result.started_at,
                    finished_at=task_result.finished_at,
                    duration_ms=task_result.duration_ms,
                    new_files=list(task_result.new_files),
                    changed_files=list(task_result.changed_files),
                    handoff_summary=handoff_summary,
                )
            )
            if task_result.exit_code != 0:
                task_run.status = "failed" if index == 1 else "partial"
                task_run.error_text = task_result.error_text or f"Subtask {subtask.subtask_id} failed"
                break

        session.active_provider = ""
        task_run.duration_ms = int((monotonic() - started_at) * 1000)
        task_run.finished_at = utc_now_iso()
        if task_run.status == "running":
            task_run.status = "success"

        synthesis_provider = "claude" if "claude" in self.container.provider_paths else (
            task_run.subtasks[-1].provider if task_run.subtasks else session.current_provider
        )
        task_run.synthesis_provider = synthesis_provider
        if task_run.subtasks:
            await self.container.ensure_runtime_started(session, synthesis_provider)
            synthesis_runtime = self.container.get_runtime(session, synthesis_provider)
            session.active_provider = synthesis_provider
            task_run.synthesis_prompt = self.build_synthesis_prompt(plan, task_run)
            synthesis_prefix = (
                "⏳ <b>Оркестратор собирает итог</b>\n\n"
                f"<b>Синтезатор:</b> <code>{escape(synthesis_provider)}</code>"
            )
            if status_callback:
                await status_callback(synthesis_prefix)
            synthesis_result = await self.execution_service.execute_provider_task(
                session=session,
                runtime=synthesis_runtime,
                provider_name=synthesis_provider,
                prompt=task_run.synthesis_prompt,
                status_callback=status_callback,
                status_prefix=synthesis_prefix,
                stream_event_callback=stream_event_callback,
            )
            if synthesis_result.exit_code == 0 and synthesis_result.answer_text.strip():
                task_run.synthesis_answer = synthesis_result.answer_text
                task_run.answer_text = synthesis_result.answer_text
                task_run.handoff_artifacts.append(self.build_handoff_summary(synthesis_result, "Synthesis"))
            else:
                task_run.answer_text = "\n\n".join(
                    f"[{subtask.provider}] {subtask.title}\n{subtask.answer_text}".strip()
                    for subtask in task_run.subtasks
                    if subtask.answer_text.strip()
                )
                if task_run.status == "success":
                    task_run.status = "partial"
                if not task_run.error_text:
                    task_run.error_text = synthesis_result.error_text or "Synthesis step failed"

        review_provider = "codex" if "codex" in self.container.provider_paths else (
            "claude" if "claude" in self.container.provider_paths else session.current_provider
        )
        task_run.review_provider = review_provider
        if task_run.answer_text.strip():
            await self.container.ensure_runtime_started(session, review_provider)
            review_runtime = self.container.get_runtime(session, review_provider)
            session.active_provider = review_provider
            task_run.review_prompt = self.build_review_prompt(plan, task_run)
            review_prefix = (
                "⏳ <b>Оркестратор выполняет review</b>\n\n"
                f"<b>Reviewer:</b> <code>{escape(review_provider)}</code>"
            )
            if status_callback:
                await status_callback(review_prefix)
            review_result = await self.execution_service.execute_provider_task(
                session=session,
                runtime=review_runtime,
                provider_name=review_provider,
                prompt=task_run.review_prompt,
                status_callback=status_callback,
                status_prefix=review_prefix,
                stream_event_callback=stream_event_callback,
            )
            if review_result.exit_code == 0 and review_result.answer_text.strip():
                task_run.review_answer = review_result.answer_text
                task_run.handoff_artifacts.append(self.build_handoff_summary(review_result, "Review"))
            elif task_run.status == "success":
                task_run.status = "partial"

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
