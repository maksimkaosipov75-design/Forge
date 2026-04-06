import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from file_manager import FileManager
from parser import LogParser
from provider_status import ProviderHealth


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TaskResult:
    provider: str = "qwen"
    prompt: str = ""
    answer_text: str = ""
    new_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    exit_code: int = 0
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""
    duration_ms: int = 0
    error_text: str = ""

    @property
    def touched_files(self) -> list[str]:
        return list(dict.fromkeys(self.new_files + self.changed_files))

    @property
    def has_details(self) -> bool:
        return bool(
            self.prompt.strip()
            or self.answer_text.strip()
            or self.new_files
            or self.changed_files
        )

    @property
    def status_emoji(self) -> str:
        return "✅" if self.exit_code == 0 else "⚠️"

    @property
    def short_status(self) -> str:
        return self.status_emoji if self.exit_code == 0 else f"{self.status_emoji} {self.exit_code}"

    @property
    def duration_text(self) -> str:
        if self.duration_ms <= 0:
            return "?"
        seconds = self.duration_ms / 1000
        if seconds < 60:
            return f"{seconds:.1f}с"
        minutes = int(seconds // 60)
        remainder = int(seconds % 60)
        return f"{minutes}м {remainder}с"

    @property
    def finished_or_started_at(self) -> str:
        return self.finished_at or self.started_at


@dataclass
class SubtaskRun:
    subtask_id: str
    title: str
    provider: str
    task_kind: str = "general"
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    answer_text: str = ""
    error_text: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    new_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    handoff_summary: str = ""
    retry_count: int = 0
    original_provider: str = ""

    @property
    def touched_files(self) -> list[str]:
        return list(dict.fromkeys(self.new_files + self.changed_files))


@dataclass
class TaskRun:
    run_id: str
    prompt: str
    mode: str = "single"
    status: str = "pending"
    strategy: str = ""
    complexity: str = "simple"
    provider_summary: str = ""
    subtasks: list[SubtaskRun] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""
    duration_ms: int = 0
    answer_text: str = ""
    error_text: str = ""
    synthesis_provider: str = ""
    synthesis_prompt: str = ""
    synthesis_answer: str = ""
    review_provider: str = ""
    review_prompt: str = ""
    review_answer: str = ""
    handoff_artifacts: list[str] = field(default_factory=list)
    artifact_file: str = ""
    ai_plan_rationale: str = ""

    @property
    def status_emoji(self) -> str:
        return "✅" if self.status == "success" else ("⚠️" if self.status in {"failed", "partial"} else "⏳")

    @property
    def duration_text(self) -> str:
        if self.duration_ms <= 0:
            return "?"
        seconds = self.duration_ms / 1000
        if seconds < 60:
            return f"{seconds:.1f}с"
        minutes = int(seconds // 60)
        remainder = int(seconds % 60)
        return f"{minutes}м {remainder}с"

    @property
    def touched_files(self) -> list[str]:
        files: list[str] = []
        for subtask in self.subtasks:
            files.extend(subtask.touched_files)
        return list(dict.fromkeys(files))

    @property
    def new_files(self) -> list[str]:
        files: list[str] = []
        for subtask in self.subtasks:
            files.extend(subtask.new_files)
        return list(dict.fromkeys(files))

    @property
    def changed_files(self) -> list[str]:
        files: list[str] = []
        for subtask in self.subtasks:
            files.extend(subtask.changed_files)
        return list(dict.fromkeys(files))

    @property
    def finished_or_started_at(self) -> str:
        return self.finished_at or self.started_at

    @classmethod
    def from_task_result(cls, task_result: TaskResult) -> "TaskRun":
        subtask = SubtaskRun(
            subtask_id="single",
            title="Single-agent execution",
            provider=task_result.provider,
            status="success" if task_result.exit_code == 0 else "failed",
            answer_text=task_result.answer_text,
            error_text=task_result.error_text,
            started_at=task_result.started_at,
            finished_at=task_result.finished_at,
            duration_ms=task_result.duration_ms,
            new_files=list(task_result.new_files),
            changed_files=list(task_result.changed_files),
        )
        return cls(
            run_id=f"run-{task_result.finished_or_started_at}",
            prompt=task_result.prompt,
            mode="single",
            status="success" if task_result.exit_code == 0 else "failed",
            strategy="single-agent execution",
            complexity="simple",
            provider_summary=task_result.provider,
            subtasks=[subtask],
            started_at=task_result.started_at,
            finished_at=task_result.finished_at,
            duration_ms=task_result.duration_ms,
            answer_text=task_result.answer_text,
            error_text=task_result.error_text,
        )


@dataclass
class QueuedTask:
    provider: str
    prompt: str
    anchor_message: Any
    status_message: Any
    mode: str = "single"
    plan: Any = None
    resume_from: int = 0
    prior_subtasks: list[Any] = field(default_factory=list)
    started: bool = False


@dataclass
class ProviderRuntime:
    provider: str
    manager: Any
    parser: LogParser
    last_file_state: dict[str, float] = field(default_factory=dict)
    health: ProviderHealth | None = None


@dataclass
class ChatSession:
    chat_id: int
    file_mgr: FileManager
    runtimes: dict[str, ProviderRuntime] = field(default_factory=dict)
    current_provider: str = "qwen"
    active_provider: str = ""
    last_file_state: dict[str, float] = field(default_factory=dict)
    last_task_result: TaskResult = field(default_factory=TaskResult)
    history: list[TaskResult] = field(default_factory=list)
    last_task_run: TaskRun | None = None
    run_history: list[TaskRun] = field(default_factory=list)
    last_plan: Any = None
    task_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending_tasks: dict[int, QueuedTask] = field(default_factory=dict)
    worker_task: asyncio.Task | None = None
    task_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
