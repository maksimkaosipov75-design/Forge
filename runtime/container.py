import os as _os
from pathlib import Path

from config import Settings, settings as default_settings
from file_manager import FileManager
from metrics import MetricsCollector
from orchestrator import AIOrchestrator, RuleBasedOrchestrator
from parser import LogParser
from process_manager import (
    ClaudeProcessManager,
    CodexProcessManager,
    QwenProcessManager,
    create_process_manager,
)
from runtime.executor import ExecutionService
from runtime.orchestrator_service import OrchestratorService
from session_store import SessionStore
from task_models import ChatSession, ProviderRuntime, ProviderStats, TaskResult, TaskRun
from providers import normalize_provider_name


class RuntimeContainer:
    def __init__(
        self,
        settings: Settings = default_settings,
        manager: QwenProcessManager | CodexProcessManager | ClaudeProcessManager | None = None,
        parser: LogParser | None = None,
        file_mgr: FileManager | None = None,
        sessions_root: Path | None = None,
    ):
        self.settings = settings
        self.manager = manager
        self.parser = parser
        self.file_mgr = file_mgr
        self.default_provider = normalize_provider_name(settings.CLI_PROVIDER)
        if manager is not None:
            if isinstance(manager, CodexProcessManager):
                self.default_provider = "codex"
            elif isinstance(manager, ClaudeProcessManager):
                self.default_provider = "claude"
            else:
                self.default_provider = "qwen"

        self.provider_paths = {
            "qwen": settings.QWEN_CLI_PATH,
            "codex": settings.CODEX_CLI_PATH,
            "claude": settings.CLAUDE_CLI_PATH,
        }
        if manager is not None:
            self.provider_paths[self.default_provider] = manager.cli_path

        self.base_projects_file = Path(file_mgr.projects_file if file_mgr else "projects.json")
        if file_mgr:
            self.base_working_dir = file_mgr.get_working_dir()
        else:
            launch_dir = _os.environ.get("FORGE_LAUNCH_DIR", "")
            self.base_working_dir = Path(launch_dir).resolve() if launch_dir else Path(_os.getcwd()).resolve()
        self.launch_dir_is_home = self.base_working_dir == Path(_os.path.expanduser("~")).resolve()
        self.sessions_root = sessions_root or Path(".session_data")
        self.sessions_root.mkdir(exist_ok=True)
        self.session_store = SessionStore(self.sessions_root)
        self.sessions: dict[int, ChatSession] = {}
        self.metrics = MetricsCollector()
        self.execution_service = ExecutionService()
        self.orchestrator_service = OrchestratorService(self, self.execution_service)

    def session_projects_file(self, chat_id: int) -> Path:
        stem = self.base_projects_file.stem or "projects"
        suffix = self.base_projects_file.suffix or ".json"
        return self.sessions_root / f"{stem}_{chat_id}{suffix}"

    def build_runtime(
        self,
        provider_name: str,
        provided_manager=None,
        provided_parser=None,
        model_name: str = "",
    ) -> ProviderRuntime:
        runtime_parser = provided_parser or LogParser()
        runtime_manager = provided_manager or create_process_manager(
            provider=provider_name,
            cli_path=self.provider_paths[provider_name],
            on_output=lambda line, target_parser=runtime_parser: target_parser.feed(line),
            model_name=model_name,
        )
        return ProviderRuntime(
            provider=provider_name,
            manager=runtime_manager,
            parser=runtime_parser,
            health=runtime_manager.health,
        )

    def build_session(self, chat_id: int) -> ChatSession:
        session_file_mgr = FileManager(projects_file=str(self.session_projects_file(chat_id)))
        session_file_mgr.working_dir = self.base_working_dir
        runtimes = {
            self.default_provider: self.build_runtime(
                self.default_provider,
                provided_manager=self.manager,
                provided_parser=self.parser,
            )
        }
        session = ChatSession(
            chat_id=chat_id,
            file_mgr=session_file_mgr,
            runtimes=runtimes,
            current_provider=self.default_provider,
        )
        self.session_store.load(session)
        return session

    def get_session(self, chat_id: int) -> ChatSession:
        session = self.sessions.get(chat_id)
        if session is None:
            session = self.build_session(chat_id)
            self.sessions[chat_id] = session
        return session

    def get_runtime(self, session: ChatSession, provider_name: str) -> ProviderRuntime:
        runtime = session.runtimes.get(provider_name)
        if runtime is None:
            model = session.provider_models.get(provider_name, "")
            runtime = self.build_runtime(provider_name, model_name=model)
            session.runtimes[provider_name] = runtime
        return runtime

    async def ensure_runtime_started(self, session: ChatSession, provider_name: str) -> ProviderRuntime:
        runtime = self.get_runtime(session, provider_name)
        if not runtime.manager.is_running:
            await runtime.manager.start()
        return runtime

    def build_planner(self, session: ChatSession | None = None) -> RuleBasedOrchestrator:
        available = list(self.provider_paths.keys())
        if session is not None:
            available = list(dict.fromkeys(list(session.runtimes.keys()) + available))
        return RuleBasedOrchestrator(available)

    def build_ai_planner(self, session: ChatSession | None = None) -> AIOrchestrator:
        available = list(self.provider_paths.keys())
        if session is not None:
            available = list(dict.fromkeys(list(session.runtimes.keys()) + available))
        fallback = RuleBasedOrchestrator(available)
        return AIOrchestrator(available, fallback)

    def pick_planning_provider(self, session: ChatSession) -> str:
        """Choose the best provider for AI planning (prefers claude > qwen > codex)."""
        for preferred in ("claude", "qwen", "codex"):
            if preferred in self.provider_paths:
                return preferred
        return session.current_provider

    def remember_task_result(self, session: ChatSession, task_result: TaskResult, retry_count: int = 0):
        self.metrics.record_task(task_result.provider, task_result.exit_code, task_result.duration_ms)
        session.last_task_result = task_result
        session.history.append(task_result)
        if len(session.history) > 10:
            session.history = session.history[-10:]
        # Update per-provider stats
        provider = task_result.provider
        if provider not in session.provider_stats:
            session.provider_stats[provider] = ProviderStats()
        session.provider_stats[provider].record(task_result, retry_count=retry_count)
        task_run = TaskRun.from_task_result(task_result)
        task_run.artifact_file = self.session_store.write_run_artifact(session, task_run)
        session.last_task_run = task_run
        session.run_history.append(task_run)
        if len(session.run_history) > 10:
            session.run_history = session.run_history[-10:]
        self.session_store.save(session)

    def save_session(self, session: ChatSession):
        self.session_store.save(session)

    def clear_session_storage(self, session: ChatSession):
        self.session_store.clear(session.chat_id)

    def latest_artifact_files(self, session: ChatSession, limit: int = 10):
        return self.session_store.latest_artifact_files(session.chat_id, limit=limit)

    def recent_runs(self, session: ChatSession, limit: int = 10) -> list[TaskRun]:
        return list(reversed(session.run_history[-limit:]))

    def run_by_index(self, session: ChatSession, entry_index: int) -> TaskRun | None:
        recent = list(reversed(session.run_history))
        if entry_index < 1 or entry_index > len(recent):
            return None
        return recent[entry_index - 1]
