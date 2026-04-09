import tempfile
import unittest
from pathlib import Path

from config import Settings
from credential_store import CredentialStore
from runtime import RuntimeContainer
from runtime.api_backends import OpenRouterExecutionBackend


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

    def test_build_runtime_for_openrouter_uses_api_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)

            runtime = container.build_runtime("openrouter")

            self.assertEqual(runtime.provider, "openrouter")
            self.assertIsInstance(runtime.manager, OpenRouterExecutionBackend)

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


if __name__ == "__main__":
    unittest.main()
