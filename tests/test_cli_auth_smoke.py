import asyncio
import tempfile
import unittest
from pathlib import Path

from credential_store import CredentialStore
from runtime import RuntimeContainer
from cli.commands import auth, smoke


class _DummyUi:
    def __init__(self):
        self.blocks = []
        self.notices = []

    def print_block(self, title, text, border_style="cyan"):
        self.blocks.append((title, text, border_style))

    def print_notice(self, message, provider="", kind="info"):
        self.notices.append((message, provider, kind))


class CliAuthSmokeTests(unittest.TestCase):
    def test_auth_without_args_saves_key_when_provided(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            ui = _DummyUi()
            args = type("Args", (), {"action": "", "target": "", "key": "secret-123", "chat_id": 0})()

            asyncio.run(auth.handle(args, container, ui))

            self.assertTrue(container.credential_store.has_api_key("openrouter"))
            self.assertEqual(ui.notices[-1][0], "Saved credentials for openrouter.")

    def test_auth_status_renders_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            ui = _DummyUi()
            args = type("Args", (), {"action": "status", "target": "", "key": "", "chat_id": 0})()

            asyncio.run(auth.handle(args, container, ui))

            self.assertEqual(len(ui.blocks), 1)
            self.assertIn("Credentials", ui.blocks[0][1])

    def test_smoke_requires_ready_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            ui = _DummyUi()
            args = type("Args", (), {"provider": "openrouter", "chat_id": 0})()

            with self.assertRaises(SystemExit) as ctx:
                asyncio.run(smoke.handle(args, container, ui))

            self.assertIn("Use `forge auth openrouter` first.", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
