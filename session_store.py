import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from orchestrator import OrchestrationPlan, PlannedSubtask
from task_models import ChatSession, SubtaskRun, TaskResult, TaskRun


log = logging.getLogger(__name__)


class SessionStore:
    def __init__(self, sessions_root: Path):
        self.sessions_root = sessions_root
        self.sessions_root.mkdir(exist_ok=True)

    def session_file(self, chat_id: int) -> Path:
        return self.sessions_root / f"chat_{chat_id}_state.json"

    def artifacts_dir(self, chat_id: int) -> Path:
        target = self.sessions_root / f"chat_{chat_id}_artifacts"
        target.mkdir(exist_ok=True)
        return target

    def load(self, session: ChatSession):
        target = self.session_file(session.chat_id)
        if not target.exists():
            return
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to load session state for chat %s: %s", session.chat_id, exc)
            return

        try:
            working_dir = payload.get("working_dir")
            if working_dir:
                session.file_mgr.working_dir = Path(working_dir)

            current_provider = payload.get("current_provider")
            if isinstance(current_provider, str) and current_provider.strip():
                session.current_provider = current_provider.strip()

            last_task_result = payload.get("last_task_result")
            if isinstance(last_task_result, dict):
                session.last_task_result = TaskResult(**last_task_result)

            session.history = [
                TaskResult(**item)
                for item in payload.get("history", [])
                if isinstance(item, dict)
            ]

            last_task_run = payload.get("last_task_run")
            if isinstance(last_task_run, dict):
                session.last_task_run = self._task_run_from_dict(last_task_run)

            session.run_history = [
                self._task_run_from_dict(item)
                for item in payload.get("run_history", [])
                if isinstance(item, dict)
            ]

            last_plan = payload.get("last_plan")
            if isinstance(last_plan, dict):
                session.last_plan = self._plan_from_dict(last_plan)
        except Exception as exc:
            log.warning("Failed to hydrate session state for chat %s: %s", session.chat_id, exc)

    def save(self, session: ChatSession):
        target = self.session_file(session.chat_id)
        payload = {
            "working_dir": str(session.file_mgr.get_working_dir()),
            "current_provider": session.current_provider,
            "last_task_result": asdict(session.last_task_result),
            "history": [asdict(item) for item in session.history[-10:]],
            "last_task_run": asdict(session.last_task_run) if session.last_task_run else None,
            "run_history": [asdict(item) for item in session.run_history[-10:]],
            "last_plan": self._plan_to_dict(session.last_plan) if session.last_plan else None,
        }
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def clear(self, chat_id: int):
        self.session_file(chat_id).unlink(missing_ok=True)
        artifacts_dir = self.artifacts_dir(chat_id)
        for path in artifacts_dir.glob("*.md"):
            path.unlink(missing_ok=True)

    def write_run_artifact(self, session: ChatSession, task_run: TaskRun) -> str:
        safe_run_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in task_run.run_id)
        target = self.artifacts_dir(session.chat_id) / f"{safe_run_id}.md"
        target.write_text(self._render_run_markdown(session, task_run), encoding="utf-8")
        return str(target)

    def latest_artifact_files(self, chat_id: int, limit: int = 10) -> list[Path]:
        return sorted(
            self.artifacts_dir(chat_id).glob("*.md"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:limit]

    @staticmethod
    def _plan_to_dict(plan: OrchestrationPlan) -> dict[str, Any]:
        return {
            "prompt": plan.prompt,
            "complexity": plan.complexity,
            "strategy": plan.strategy,
            "subtasks": [
                {
                    "subtask_id": item.subtask_id,
                    "title": item.title,
                    "description": item.description,
                    "task_kind": item.task_kind,
                    "suggested_provider": item.suggested_provider,
                    "reason": item.reason,
                    "depends_on": list(item.depends_on),
                }
                for item in plan.subtasks
            ],
        }

    @staticmethod
    def _plan_from_dict(payload: dict[str, Any]) -> OrchestrationPlan:
        return OrchestrationPlan(
            prompt=payload.get("prompt", ""),
            complexity=payload.get("complexity", "simple"),
            strategy=payload.get("strategy", ""),
            subtasks=[
                PlannedSubtask(
                    subtask_id=item.get("subtask_id", ""),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    task_kind=item.get("task_kind", "general"),
                    suggested_provider=item.get("suggested_provider", "qwen"),
                    reason=item.get("reason", ""),
                    depends_on=list(item.get("depends_on", [])),
                )
                for item in payload.get("subtasks", [])
                if isinstance(item, dict)
            ],
        )

    @staticmethod
    def _task_run_from_dict(payload: dict[str, Any]) -> TaskRun:
        subtasks = [
            SubtaskRun(**item)
            for item in payload.get("subtasks", [])
            if isinstance(item, dict)
        ]
        clone = dict(payload)
        clone["subtasks"] = subtasks
        return TaskRun(**clone)

    @staticmethod
    def _render_run_markdown(session: ChatSession, task_run: TaskRun) -> str:
        lines = [
            f"# Run {task_run.run_id}",
            "",
            f"- Status: {task_run.status}",
            f"- Mode: {task_run.mode}",
            f"- Provider summary: {task_run.provider_summary or 'mixed'}",
            f"- Complexity: {task_run.complexity}",
            f"- Duration: {task_run.duration_text}",
            f"- Working dir: {session.file_mgr.get_working_dir()}",
            "",
            "## Prompt",
            "",
            "```text",
            task_run.prompt,
            "```",
            "",
        ]
        if task_run.strategy:
            lines.extend(["## Strategy", "", task_run.strategy, ""])
        if task_run.subtasks:
            lines.extend(["## Subtasks", ""])
            for item in task_run.subtasks:
                lines.extend([
                    f"### {item.subtask_id} · {item.title}",
                    "",
                    f"- Provider: {item.provider}",
                    f"- Status: {item.status}",
                    f"- Kind: {item.task_kind}",
                    f"- Duration: {item.duration_ms}ms",
                    "",
                ])
                if item.handoff_summary:
                    lines.extend(["#### Handoff", "", "```text", item.handoff_summary, "```", ""])
                if item.answer_text:
                    lines.extend(["#### Answer", "", "```text", item.answer_text[:6000], "```", ""])
                if item.error_text:
                    lines.extend(["#### Error", "", "```text", item.error_text[:3000], "```", ""])
        if task_run.handoff_artifacts:
            lines.extend(["## Handoff Artifacts", ""])
            for index, artifact in enumerate(task_run.handoff_artifacts, start=1):
                lines.extend([f"### Artifact {index}", "", "```text", artifact[:6000], "```", ""])
        if task_run.synthesis_provider:
            lines.extend([
                "## Synthesis",
                "",
                f"- Provider: {task_run.synthesis_provider}",
                "",
            ])
            if task_run.synthesis_answer:
                lines.extend(["```text", task_run.synthesis_answer[:8000], "```", ""])
        if task_run.review_provider:
            lines.extend([
                "## Review",
                "",
                f"- Provider: {task_run.review_provider}",
                "",
            ])
            if task_run.review_answer:
                lines.extend(["```text", task_run.review_answer[:8000], "```", ""])
        if task_run.answer_text:
            lines.extend(["## Final Answer", "", "```text", task_run.answer_text[:12000], "```", ""])
        return "\n".join(lines).strip() + "\n"
