import asyncio
import os as _os
from pathlib import Path

from core.config import Settings, settings as default_settings
from core.credential_store import CredentialStore
from core.file_manager import FileManager
from core.metrics import MetricsCollector
from core.openrouter_catalog import ModelResolveResult, OpenRouterModelCatalog
from core.orchestrator import AIOrchestrator, RuleBasedOrchestrator
from core.parser import LogParser
from core.process_manager import (
    ClaudeProcessManager,
    CodexProcessManager,
    QwenProcessManager,
    create_process_manager,
)
from core.providers import get_provider_definition, is_api_provider, normalize_provider_name, provider_default_model
from runtime.api_backends import OpenRouterExecutionBackend
from runtime.executor import ExecutionService
from runtime.orchestrator_service import OrchestratorService
from core.session_store import SessionStore
from core.task_models import ChatSession, ProviderRuntime, ProviderStats, TaskResult, TaskRun
from core.providers import list_provider_models


class RuntimeContainer:
    def __init__(
        self,
        settings: Settings = default_settings,
        manager: QwenProcessManager | CodexProcessManager | ClaudeProcessManager | None = None,
        parser: LogParser | None = None,
        file_mgr: FileManager | None = None,
        sessions_root: Path | None = None,
        credential_store: CredentialStore | None = None,
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
            "openrouter": settings.OPENROUTER_BASE_URL,
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
        self.credential_store = credential_store or CredentialStore()
        self.openrouter_catalog = OpenRouterModelCatalog(
            cache_path=self.sessions_root / "openrouter_models.json",
            base_url=self.settings.OPENROUTER_BASE_URL,
            timeout=self.settings.OPENROUTER_MODELS_HTTP_TIMEOUT,
            ttl_seconds=self.settings.OPENROUTER_MODEL_CACHE_TTL_SECONDS,
            api_key_getter=lambda: self.resolve_api_key("openrouter"),
        )

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
        normalized_provider = normalize_provider_name(provider_name)
        selected_model = model_name or provider_default_model(normalized_provider)
        if provided_manager is not None:
            runtime_manager = provided_manager
        elif is_api_provider(normalized_provider):
            definition = get_provider_definition(normalized_provider)
            runtime_manager = OpenRouterExecutionBackend(
                api_key=self.resolve_api_key(normalized_provider),
                base_url=self.settings.OPENROUTER_BASE_URL,
                on_output=lambda line, target_parser=runtime_parser: target_parser.feed(line),
                model_name=selected_model or definition.default_model,
                timeout=self.settings.OPENROUTER_HTTP_TIMEOUT,
                app_name="Forge",
            )
        else:
            runtime_manager = create_process_manager(
                provider=normalized_provider,
                cli_path=self.provider_paths[normalized_provider],
                on_output=lambda line, target_parser=runtime_parser: target_parser.feed(line),
                model_name=selected_model,
            )
        return ProviderRuntime(
            provider=normalized_provider,
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
            model = self.resolve_provider_model(session, provider_name)
            runtime = self.build_runtime(provider_name, model_name=model)
            session.runtimes[provider_name] = runtime
        return runtime

    def resolve_provider_model(self, session: ChatSession, provider_name: str) -> str:
        configured = session.provider_models.get(provider_name, "").strip()
        return configured or provider_default_model(provider_name)

    def list_available_models(self, provider_name: str, refresh: bool = False):
        normalized = normalize_provider_name(provider_name)
        if normalized == "openrouter":
            return self.openrouter_catalog.list_models(refresh=refresh)
        return list_provider_models(normalized)

    def resolve_model_selection(self, provider_name: str, query: str, refresh: bool = False) -> ModelResolveResult:
        normalized = normalize_provider_name(provider_name)
        if normalized == "openrouter":
            return self.openrouter_catalog.resolve_model(query, refresh=refresh)
        cleaned = (query or "").strip()
        if not cleaned:
            return ModelResolveResult(status="empty", message="No model query provided.")
        if cleaned.lower() == "default":
            return ModelResolveResult(status="exact", model_name="")
        return ModelResolveResult(status="raw", model_name=cleaned, message="Using the exact model name you entered.")

    def provider_is_ready(self, provider_name: str) -> tuple[bool, str]:
        normalized = normalize_provider_name(provider_name)
        if normalized == "openrouter" and not self.resolve_api_key(normalized).strip():
            return False, "OpenRouter API key is not configured."
        return True, ""

    def resolve_api_key(self, provider_name: str) -> str:
        normalized = normalize_provider_name(provider_name)
        if normalized == "openrouter":
            return self.settings.OPENROUTER_API_KEY.strip() or self.credential_store.get_api_key(normalized).strip()
        return ""

    def reset_runtime(self, session: ChatSession, provider_name: str):
        runtime = session.runtimes.pop(provider_name, None)
        if runtime and runtime.manager.is_running:
            try:
                asyncio.create_task(runtime.manager.stop())
            except Exception:
                pass

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
        """Choose the best provider for AI planning (prefers openrouter > claude > qwen > codex)."""
        for preferred in ("openrouter", "claude", "qwen", "codex"):
            if preferred not in self.provider_paths:
                continue
            ready, _ = self.provider_is_ready(preferred)
            if ready:
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
