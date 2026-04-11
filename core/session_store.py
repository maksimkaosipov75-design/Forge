import json
import logging
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from core.orchestrator import OrchestrationPlan, PlannedSubtask
from core.provider_status import ProviderHealth
from core.task_models import ChatSession, ProviderStats, SubtaskRun, TaskResult, TaskRun


log = logging.getLogger(__name__)

MAX_ARTIFACTS = 50      # per-session artifact file limit
MAX_HISTORY = 10        # task result / run history entries kept


class SessionStore:
    def __init__(self, sessions_root: Path):
        self.sessions_root = sessions_root
        self.sessions_root.mkdir(exist_ok=True)
        self.db_path = self.sessions_root / "session_store.sqlite3"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_state (
                    chat_id INTEGER PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    chat_id INTEGER PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def session_file(self, chat_id: int) -> Path:
        return self.sessions_root / f"chat_{chat_id}_state.json"

    def artifacts_dir(self, chat_id: int) -> Path:
        target = self.sessions_root / f"chat_{chat_id}_artifacts"
        target.mkdir(exist_ok=True)
        return target

    def load(self, session: ChatSession):
        payload = self._load_session_payload(session.chat_id)
        if payload is None:
            return

        try:
            # working_dir is intentionally NOT restored — always starts at Path.home()

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

            # Provider stats
            for name, sdata in payload.get("provider_stats", {}).items():
                if isinstance(sdata, dict):
                    s = ProviderStats()
                    s.total_tasks = int(sdata.get("total_tasks", 0))
                    s.successful_tasks = int(sdata.get("successful_tasks", 0))
                    s.failed_tasks = int(sdata.get("failed_tasks", 0))
                    s.retry_count = int(sdata.get("retry_count", 0))
                    s.total_ms = int(sdata.get("total_ms", 0))
                    session.provider_stats[name] = s

            # Provider health
            for name, hdata in payload.get("provider_health", {}).items():
                if isinstance(hdata, dict):
                    h = ProviderHealth.from_dict(hdata)
                    # Apply health to existing runtime if present
                    runtime = session.runtimes.get(name)
                    if runtime is not None:
                        runtime.health = h
                    # Store as standalone reference for later use
                    session.provider_health_cache[name] = h

            # Provider model selections
            pm = payload.get("provider_models")
            if isinstance(pm, dict):
                session.provider_models = {k: v for k, v in pm.items() if isinstance(v, str)}

            preferences = payload.get("ui_preferences")
            if isinstance(preferences, dict):
                session.ui_preferences = {k: v for k, v in preferences.items() if isinstance(v, str)}

        except Exception as exc:
            log.warning("Failed to hydrate session state for chat %s: %s", session.chat_id, exc)

    def save(self, session: ChatSession):
        # Collect current health from runtimes
        health_data: dict[str, dict] = {}
        for name, runtime in session.runtimes.items():
            if runtime.health is not None:
                health_data[name] = runtime.health.to_dict()
        # Also include cached health for providers that don't have a runtime yet
        for name, h in getattr(session, "provider_health_cache", {}).items():
            if name not in health_data:
                health_data[name] = h.to_dict()

        stats_data = {
            name: {
                "total_tasks": s.total_tasks,
                "successful_tasks": s.successful_tasks,
                "failed_tasks": s.failed_tasks,
                "retry_count": s.retry_count,
                "total_ms": s.total_ms,
            }
            for name, s in session.provider_stats.items()
        }

        payload = {
            "current_provider": session.current_provider,
            "provider_models": dict(session.provider_models),
            "ui_preferences": dict(session.ui_preferences),
            "last_task_result": asdict(session.last_task_result),
            "history": [asdict(item) for item in session.history[-MAX_HISTORY:]],
            "last_task_run": asdict(session.last_task_run) if session.last_task_run else None,
            "run_history": [asdict(item) for item in session.run_history[-MAX_HISTORY:]],
            "last_plan": self._plan_to_dict(session.last_plan) if session.last_plan else None,
            "provider_stats": stats_data,
            "provider_health": health_data,
        }
        self._save_session_payload(session.chat_id, payload)

    def clear(self, chat_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM chat_state WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM checkpoints WHERE chat_id = ?", (chat_id,))
            conn.commit()
        self.session_file(chat_id).unlink(missing_ok=True)
        artifacts_dir = self.artifacts_dir(chat_id)
        for path in artifacts_dir.glob("*.md"):
            path.unlink(missing_ok=True)

    def write_run_artifact(self, session: ChatSession, task_run: TaskRun) -> str:
        safe_run_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in task_run.run_id)
        target = self.artifacts_dir(session.chat_id) / f"{safe_run_id}.md"
        target.write_text(self._render_run_markdown(session, task_run), encoding="utf-8")
        self._prune_artifacts(session.chat_id)
        return str(target)

    def checkpoint_file(self, chat_id: int) -> Path:
        return self.sessions_root / f"chat_{chat_id}_checkpoint.json"

    def write_checkpoint(self, session: "ChatSession", task_run: "TaskRun"):
        """Save in-progress TaskRun state after each subtask for crash recovery."""
        payload = asdict(task_run)
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO checkpoints (chat_id, payload_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (session.chat_id, json.dumps(payload, ensure_ascii=False)),
                )
                conn.commit()
        except Exception as exc:
            log.warning("Failed to write checkpoint for chat %s: %s", session.chat_id, exc)

    def load_checkpoint(self, session: "ChatSession") -> "TaskRun | None":
        """Load the checkpoint for crash recovery. Returns None if not found."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM checkpoints WHERE chat_id = ?",
                    (session.chat_id,),
                ).fetchone()
            if row is not None:
                data = json.loads(row["payload_json"])
                return self._task_run_from_dict(data)
            target = self.checkpoint_file(session.chat_id)
            if target.exists():
                data = json.loads(target.read_text(encoding="utf-8"))
                self._save_checkpoint_payload(session.chat_id, data)
                return self._task_run_from_dict(data)
            return None
        except Exception as exc:
            log.warning("Failed to load checkpoint for chat %s: %s", session.chat_id, exc)
            return None

    def clear_checkpoint(self, chat_id: int):
        """Remove the checkpoint file after successful completion or manual discard."""
        with self._connect() as conn:
            conn.execute("DELETE FROM checkpoints WHERE chat_id = ?", (chat_id,))
            conn.commit()
        self.checkpoint_file(chat_id).unlink(missing_ok=True)

    def _load_session_payload(self, chat_id: int) -> dict[str, Any] | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload_json FROM chat_state WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
            if row is not None:
                return json.loads(row["payload_json"])
        except Exception as exc:
            log.warning("Failed to load session state for chat %s from sqlite: %s", chat_id, exc)

        target = self.session_file(chat_id)
        if not target.exists():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            self._save_session_payload(chat_id, payload)
            return payload
        except Exception as exc:
            log.warning("Failed to load session state for chat %s: %s", chat_id, exc)
            return None

    def _save_session_payload(self, chat_id: int, payload: dict[str, Any]):
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO chat_state (chat_id, payload_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (chat_id, json.dumps(payload, ensure_ascii=False)),
                )
                conn.commit()
        except Exception as exc:
            log.warning("Failed to save session state for chat %s into sqlite: %s", chat_id, exc)
            target = self.session_file(chat_id)
            target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _save_checkpoint_payload(self, chat_id: int, payload: dict[str, Any]):
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (chat_id, payload_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (chat_id, json.dumps(payload, ensure_ascii=False)),
            )
            conn.commit()

    def _prune_artifacts(self, chat_id: int):
        """Delete oldest artifacts beyond MAX_ARTIFACTS."""
        all_artifacts = sorted(
            self.artifacts_dir(chat_id).glob("*.md"),
            key=lambda p: p.stat().st_mtime,
        )
        excess = len(all_artifacts) - MAX_ARTIFACTS
        for old in all_artifacts[:excess]:
            try:
                old.unlink()
            except OSError:
                pass

    def latest_artifact_files(self, chat_id: int, limit: int = 10) -> list[Path]:
        return sorted(
            self.artifacts_dir(chat_id).glob("*.md"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:limit]

    # ------------------------------------------------------------------ #
    # Serialisation helpers                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _plan_to_dict(plan: OrchestrationPlan) -> dict[str, Any]:
        return {
            "prompt": plan.prompt,
            "complexity": plan.complexity,
            "strategy": plan.strategy,
            "ai_rationale": plan.ai_rationale,
            "subtasks": [
                {
                    "subtask_id": item.subtask_id,
                    "title": item.title,
                    "description": item.description,
                    "task_kind": item.task_kind,
                    "suggested_provider": item.suggested_provider,
                    "reason": item.reason,
                    "depends_on": list(item.depends_on),
                    "parallel_group": item.parallel_group,
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
            ai_rationale=payload.get("ai_rationale", ""),
            subtasks=[
                PlannedSubtask(
                    subtask_id=item.get("subtask_id", ""),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    task_kind=item.get("task_kind", "general"),
                    suggested_provider=item.get("suggested_provider", "qwen"),
                    reason=item.get("reason", ""),
                    depends_on=list(item.get("depends_on", [])),
                    parallel_group=int(item.get("parallel_group", 0)),
                )
                for item in payload.get("subtasks", [])
                if isinstance(item, dict)
            ],
        )

    @staticmethod
    def _task_run_from_dict(payload: dict[str, Any]) -> TaskRun:
        subtasks = []
        for item in payload.get("subtasks", []):
            if not isinstance(item, dict):
                continue
            # Guard unknown fields introduced in newer versions
            known = {f.name for f in SubtaskRun.__dataclass_fields__.values()}
            filtered = {k: v for k, v in item.items() if k in known}
            subtasks.append(SubtaskRun(**filtered))
        clone = {k: v for k, v in payload.items() if k != "subtasks"}
        known_run = {f.name for f in TaskRun.__dataclass_fields__.values()}
        clone = {k: v for k, v in clone.items() if k in known_run}
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
            f"- Model summary: {task_run.model_summary or 'n/a'}",
            f"- Transport summary: {task_run.transport_summary or 'n/a'}",
            f"- Complexity: {task_run.complexity}",
            f"- Duration: {task_run.duration_text}",
            f"- Tokens: {task_run.total_input_tokens} in / {task_run.total_output_tokens} out",
            f"- Working dir: {session.file_mgr.get_working_dir()}",
        ]
        if task_run.ai_plan_rationale:
            lines += [f"- AI rationale: {task_run.ai_plan_rationale}"]
        lines += ["", "## Prompt", "", "```text", task_run.prompt, "```", ""]
        if task_run.strategy:
            lines.extend(["## Strategy", "", task_run.strategy, ""])
        if task_run.subtasks:
            lines.extend(["## Subtasks", ""])
            for item in task_run.subtasks:
                retry_note = f"  (retry #{item.retry_count} from {item.original_provider})" if item.retry_count else ""
                lines.extend([
                    f"### {item.subtask_id} · {item.title}",
                    "",
                    f"- Provider: {item.provider}{retry_note}",
                    f"- Model: {item.model_name or 'default'}",
                    f"- Transport: {item.transport or 'unknown'}",
                    f"- Status: {item.status}",
                    f"- Kind: {item.task_kind}",
                    f"- Duration: {item.duration_ms}ms",
                    f"- Tokens: {item.input_tokens} in / {item.output_tokens} out",
                    "",
                ])
                if item.handoff_summary:
                    lines.extend(["#### Handoff", "", "```text", item.handoff_summary, "```", ""])
                if item.handoff_record:
                    lines.extend(["#### Handoff Record", "", "```json", json.dumps(item.handoff_record, ensure_ascii=False, indent=2)[:6000], "```", ""])
                if item.answer_text:
                    lines.extend(["#### Answer", "", "```text", item.answer_text[:6000], "```", ""])
                if item.error_text:
                    lines.extend(["#### Error", "", "```text", item.error_text[:3000], "```", ""])
        if task_run.handoff_artifacts:
            lines.extend(["## Handoff Artifacts", ""])
            for index, artifact in enumerate(task_run.handoff_artifacts, start=1):
                lines.extend([f"### Artifact {index}", "", "```text", artifact[:6000], "```", ""])
        if task_run.handoff_records:
            lines.extend(["## Structured Handoffs", ""])
            for index, artifact in enumerate(task_run.handoff_records, start=1):
                lines.extend([f"### Handoff {index}", "", "```json", json.dumps(artifact, ensure_ascii=False, indent=2)[:6000], "```", ""])
        if task_run.synthesis_provider:
            lines.extend([
                "## Synthesis", "",
                f"- Provider: {task_run.synthesis_provider}",
                f"- Model: {task_run.synthesis_model or 'default'}",
                f"- Transport: {task_run.synthesis_transport or 'unknown'}",
                "",
            ])
            if task_run.synthesis_answer:
                lines.extend(["```text", task_run.synthesis_answer[:8000], "```", ""])
        if task_run.review_provider:
            lines.extend([
                "## Review", "",
                f"- Provider: {task_run.review_provider}",
                f"- Model: {task_run.review_model or 'default'}",
                f"- Transport: {task_run.review_transport or 'unknown'}",
                "",
            ])
            if task_run.review_answer:
                lines.extend(["```text", task_run.review_answer[:8000], "```", ""])
        if task_run.answer_text:
            lines.extend(["## Final Answer", "", "```text", task_run.answer_text[:12000], "```", ""])
        return "\n".join(lines).strip() + "\n"
