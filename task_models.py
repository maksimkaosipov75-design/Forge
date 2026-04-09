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
    model_name: str = ""
    transport: str = "cli"
    prompt: str = ""
    answer_text: str = ""
    new_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
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
    model_name: str = ""
    transport: str = "cli"
    task_kind: str = "general"
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    answer_text: str = ""
    error_text: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    new_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    handoff_summary: str = ""
    handoff_record: dict[str, Any] = field(default_factory=dict)
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
    model_summary: str = ""
    transport_summary: str = ""
    subtasks: list[SubtaskRun] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    answer_text: str = ""
    error_text: str = ""
    synthesis_provider: str = ""
    synthesis_model: str = ""
    synthesis_transport: str = ""
    synthesis_prompt: str = ""
    synthesis_answer: str = ""
    review_provider: str = ""
    review_model: str = ""
    review_transport: str = ""
    review_prompt: str = ""
    review_answer: str = ""
    handoff_artifacts: list[str] = field(default_factory=list)
    handoff_records: list[dict[str, Any]] = field(default_factory=list)
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
            model_name=task_result.model_name,
            transport=task_result.transport,
            status="success" if task_result.exit_code == 0 else "failed",
            answer_text=task_result.answer_text,
            error_text=task_result.error_text,
            started_at=task_result.started_at,
            finished_at=task_result.finished_at,
            duration_ms=task_result.duration_ms,
            input_tokens=task_result.input_tokens,
            output_tokens=task_result.output_tokens,
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
            model_summary=task_result.model_name,
            transport_summary=task_result.transport,
            subtasks=[subtask],
            started_at=task_result.started_at,
            finished_at=task_result.finished_at,
            duration_ms=task_result.duration_ms,
            input_tokens=task_result.input_tokens,
            output_tokens=task_result.output_tokens,
            total_input_tokens=task_result.total_input_tokens,
            total_output_tokens=task_result.total_output_tokens,
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
class ProviderStats:
    """Cumulative per-provider performance stats tracked across the session."""
    total_tasks: int = 0
    successful_tasks: int = 0
    failed_tasks: int = 0
    retry_count: int = 0
    total_ms: int = 0

    def record(self, result: "TaskResult", retry_count: int = 0):
        self.total_tasks += 1
        if result.exit_code == 0:
            self.successful_tasks += 1
        else:
            self.failed_tasks += 1
        self.retry_count += retry_count
        self.total_ms += result.duration_ms

    @property
    def avg_ms(self) -> int:
        return self.total_ms // self.total_tasks if self.total_tasks else 0

    @property
    def success_rate(self) -> float:
        return self.successful_tasks / self.total_tasks if self.total_tasks else 0.0


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
    provider_stats: dict[str, "ProviderStats"] = field(default_factory=dict)
    # User-selected model per provider (e.g. {"qwen": "qwen-coder-plus", "codex": "o3-mini"})
    # Empty string means "use the provider's default model"
    provider_models: dict[str, str] = field(default_factory=dict)
    # Persisted health snapshots for providers not yet instantiated as runtimes
    provider_health_cache: dict[str, Any] = field(default_factory=dict)
    task_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending_tasks: dict[int, QueuedTask] = field(default_factory=dict)
    worker_task: asyncio.Task | None = None
    task_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
