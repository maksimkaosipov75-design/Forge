import asyncio
import tempfile
import unittest
from pathlib import Path

from core.config import Settings
from core.credential_store import CredentialStore
from core.file_manager import FileManager
from core.parser import LogParser
from core.provider_status import ProviderHealth
from core.task_models import ChatSession, ProviderRuntime, TaskResult
from runtime import RuntimeContainer
from runtime.api_backends import LocalLLMExecutionBackend, OpenRouterExecutionBackend
from runtime.executor import ExecutionService, _configure_api_manager


class HangingManager:
    provider_name = "openrouter"
    model_name = "bad-model"
    startup_timeout = 1

    def __init__(self):
        self.health = ProviderHealth(provider=self.provider_name)
        self.stopped = False
        self.failure_text = ""
        self._stream_callback = None
        self._final_result_callback = None

    def set_stream_callback(self, callback):
        self._stream_callback = callback

    def set_final_result_callback(self, callback):
        self._final_result_callback = callback

    async def send_command(self, text: str, cwd: Path = None):
        await asyncio.Event().wait()
        return 0

    async def stop(self):
        self.stopped = True

    async def write_stdin(self, text: str) -> bool:
        return False

    def mark_failure(self, text: str):
        self.failure_text = text
        self.health.last_error = text


class RunningManager:
    def __init__(self):
        self.is_running = True
        self.stopped = False
        self.health = ProviderHealth(provider="qwen")

    async def stop(self):
        self.stopped = True


class RuntimeContainerTests(unittest.TestCase):
    def test_build_planner_and_session_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            session = container.get_session(100)
            planner = container.build_planner(session)
            plan = planner.build_plan("Build GTK UI with Rust backend and Python parser")

            self.assertEqual(session.chat_id, 100)
            self.assertGreaterEqual(len(plan.subtasks), 2)
            self.assertIn("qwen", container.provider_paths)
            self.assertIn("openrouter", container.provider_paths)
            self.assertIn("local", container.provider_paths)

    def test_build_runtime_for_openrouter_uses_api_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)

            runtime = container.build_runtime("openrouter")

            self.assertEqual(runtime.provider, "openrouter")
            self.assertIsInstance(runtime.manager, OpenRouterExecutionBackend)

    def test_build_runtime_for_local_uses_local_api_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)

            runtime = container.build_runtime("local")

            self.assertEqual(runtime.provider, "local")
            self.assertIsInstance(runtime.manager, LocalLLMExecutionBackend)
            self.assertEqual(runtime.manager.provider_name, "local")
            self.assertFalse(runtime.manager.tools_enabled)
            self.assertGreaterEqual(runtime.manager.startup_timeout, 300)
            self.assertFalse(runtime.manager.prefer_streaming)

    def test_build_runtime_for_local_can_enable_tools_from_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings()
            settings.LOCAL_LLM_ENABLE_TOOLS = True
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(settings=settings, sessions_root=Path(tmpdir), credential_store=store)

            runtime = container.build_runtime("local")

            self.assertTrue(runtime.manager.tools_enabled)

    def test_build_runtime_for_local_can_enable_streaming_from_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings()
            settings.LOCAL_LLM_ENABLE_STREAMING = True
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(settings=settings, sessions_root=Path(tmpdir), credential_store=store)

            runtime = container.build_runtime("local")

            self.assertTrue(runtime.manager.prefer_streaming)

    def test_local_chat_only_runtime_gets_direct_answer_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_mgr = FileManager(projects_file=str(Path(tmpdir) / "projects.json"))
            file_mgr.set_working_dir(tmpdir)
            session = ChatSession(chat_id=100, file_mgr=file_mgr)
            session.history.append(
                TaskResult(
                    provider="local",
                    prompt="previous",
                    answer_text="ого2232324124123232132",
                )
            )
            backend = LocalLLMExecutionBackend(
                base_url="http://127.0.0.1:11434/v1",
                on_output=lambda _line: None,
                model_name="llama3.1:8b",
            )

            _configure_api_manager(backend, session, "hello")

            self.assertEqual(backend.conversation_history, [])
            self.assertIn("Answer the user's message directly", backend.project_context)
            self.assertNotIn("full tool access", backend.project_context)
            self.assertNotIn("Project structure", backend.project_context)

    def test_local_runtime_uses_configured_startup_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings()
            settings.LOCAL_LLM_STARTUP_TIMEOUT = 777
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(settings=settings, sessions_root=Path(tmpdir), credential_store=store)

            runtime = container.build_runtime("local")

            self.assertEqual(runtime.manager.startup_timeout, 777)

    def test_local_runtime_uses_configured_disable_thinking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings()
            settings.LOCAL_LLM_DISABLE_THINKING = False
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(settings=settings, sessions_root=Path(tmpdir), credential_store=store)

            runtime = container.build_runtime("local")

            self.assertFalse(runtime.manager.disable_thinking)

    def test_pick_planning_provider_prefers_openrouter_when_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings()
            settings.OPENROUTER_API_KEY = "test-key"
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(settings=settings, sessions_root=Path(tmpdir), credential_store=store)
            session = container.get_session(100)

            self.assertEqual(container.pick_planning_provider(session), "openrouter")

    def test_pick_planning_provider_skips_openrouter_without_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings()
            settings.OPENROUTER_API_KEY = ""
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(settings=settings, sessions_root=Path(tmpdir), credential_store=store)
            session = container.get_session(100)

            self.assertNotEqual(container.pick_planning_provider(session), "openrouter")

    def test_provider_is_ready_uses_saved_credential_store_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings()
            settings.OPENROUTER_API_KEY = ""
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            store.set_api_key("openrouter", "saved-key")
            container = RuntimeContainer(
                settings=settings,
                sessions_root=Path(tmpdir),
                credential_store=store,
            )

            ready, message = container.provider_is_ready("openrouter")

            self.assertTrue(ready)
            self.assertEqual(message, "")

    def test_provider_is_ready_accepts_local_without_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings()
            settings.LOCAL_LLM_API_KEY = ""
            settings.LOCAL_LLM_BASE_URL = "http://127.0.0.1:11434/v1"
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(
                settings=settings,
                sessions_root=Path(tmpdir),
                credential_store=store,
            )

            ready, message = container.provider_is_ready("local")

            self.assertTrue(ready)
            self.assertEqual(message, "")

    def test_provider_is_ready_reports_missing_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings()
            settings.CODEX_CLI_PATH = "definitely-missing-forge-codex"
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(
                settings=settings,
                sessions_root=Path(tmpdir),
                credential_store=store,
            )

            ready, message = container.provider_is_ready("codex")

            self.assertFalse(ready)
            self.assertIn("codex CLI not found", message)

    def test_validate_provider_model_rejects_cross_provider_cli_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            session = container.get_session(100)
            session.provider_models["claude"] = "gpt-5.3-codex"

            ok, message = container.validate_provider_model(session, "claude")

            self.assertFalse(ok)
            self.assertIn("does not look compatible", message)

    def test_validate_provider_model_rejects_missing_openrouter_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            session = container.get_session(100)
            session.provider_models["openrouter"] = "gpt-5.3-codex"

            ok, message = container.validate_provider_model(session, "openrouter")

            self.assertFalse(ok)
            self.assertIn("No OpenRouter model matched", message)

    def test_resolve_cli_model_aliases_from_catalog(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)

            claude = container.resolve_model_selection("claude", "sonnet")
            codex = container.resolve_model_selection("codex", "mini")

            self.assertEqual(claude.status, "exact")
            self.assertEqual(claude.model_name, "claude-sonnet-4-6")
            self.assertEqual(codex.status, "exact")
            self.assertEqual(codex.model_name, "gpt-5.4-mini")

    def test_textual_app_checks_model_before_starting_runtime(self):
        source = (Path(__file__).resolve().parents[1] / "cli/textual_app.py").read_text(encoding="utf-8")
        prompt_runner = source[source.index("async def _run_prompt_inner") :]

        self.assertIn("container.validate_provider_model(session, provider_name)", prompt_runner)
        self.assertIn("asyncio.wait_for(", prompt_runner)
        self.assertLess(
            prompt_runner.index("container.validate_provider_model(session, provider_name)"),
            prompt_runner.index("container.ensure_runtime_started(session, provider_name)"),
        )

    def test_execution_service_fails_hung_startup_instead_of_staying_starting(self):
        async def run_case():
            with tempfile.TemporaryDirectory() as tmpdir:
                file_mgr = FileManager(projects_file=str(Path(tmpdir) / "projects.json"))
                file_mgr.set_working_dir(tmpdir)
                session = ChatSession(chat_id=100, file_mgr=file_mgr)
                manager = HangingManager()
                runtime = ProviderRuntime(
                    provider="openrouter",
                    manager=manager,
                    parser=LogParser(),
                    health=manager.health,
                )
                events: list[str] = []

                result = await ExecutionService().execute_provider_task(
                    session,
                    runtime,
                    "openrouter",
                    "hello",
                    stream_event_callback=events.append,
                )

                self.assertEqual(result.exit_code, -1)
                self.assertTrue(manager.stopped)
                self.assertIn("No provider response after", result.error_text)
                self.assertIn("No provider response after", manager.failure_text)
                self.assertTrue(any("No provider response after" in event for event in events))

        asyncio.run(run_case())

    def test_reset_runtime_stops_manager_without_running_loop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            session = container.get_session(100)
            manager = RunningManager()
            runtime = ProviderRuntime(
                provider="qwen",
                manager=manager,
                parser=LogParser(),
                health=manager.health,
            )
            session.runtimes["qwen"] = runtime

            container.reset_runtime(session, "qwen")

            self.assertTrue(manager.stopped)
            self.assertNotIn("qwen", session.runtimes)


if __name__ == "__main__":
    unittest.main()
