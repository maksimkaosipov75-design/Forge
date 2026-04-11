import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from core.credential_store import CredentialStore
from runtime import RuntimeContainer
from cli.commands import model


class _DummyUi:
    def __init__(self):
        self.blocks = []
        self.notices = []

    def print_block(self, title, text, border_style="cyan"):
        self.blocks.append((title, text, border_style))

    def print_notice(self, message, provider="", kind="info"):
        self.notices.append((message, provider, kind))


class CliModelCommandTests(unittest.TestCase):
    def test_model_command_resolves_openrouter_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "openrouter_models.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": 9999999999,
                        "models": [
                            {
                                "name": "anthropic/claude-sonnet-4",
                                "label": "Claude Sonnet 4",
                                "description": "Balanced Anthropic model",
                                "aliases": ["sonnet", "claude sonnet"],
                                "context_length": 200000,
                                "prompt_price": "0.000003",
                                "completion_price": "0.000015",
                                "featured": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            ui = _DummyUi()
            args = type("Args", (), {"provider": "openrouter", "model": "sonnet", "chat_id": 0})()

            asyncio.run(model.handle(args, container, ui))

            session = container.get_session(0)
            self.assertEqual(session.provider_models["openrouter"], "anthropic/claude-sonnet-4")
            self.assertIn("openrouter model set to anthropic/claude-sonnet-4", ui.notices[-1][0])

    def test_model_command_shows_matches_for_ambiguous_openrouter_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "openrouter_models.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "fetched_at": 9999999999,
                        "models": [
                            {
                                "name": "anthropic/claude-sonnet-4",
                                "label": "Claude Sonnet 4",
                                "description": "Balanced Anthropic model",
                                "aliases": ["sonnet", "claude sonnet"],
                                "context_length": 200000,
                                "prompt_price": "0.000003",
                                "completion_price": "0.000015",
                                "featured": False,
                            },
                            {
                                "name": "anthropic/claude-3.7-sonnet",
                                "label": "Claude 3.7 Sonnet",
                                "description": "Another Sonnet-family model",
                                "aliases": ["sonnet", "claude sonnet"],
                                "context_length": 200000,
                                "prompt_price": "0.000003",
                                "completion_price": "0.000015",
                                "featured": False,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            ui = _DummyUi()
            args = type("Args", (), {"provider": "openrouter", "model": "sonnet", "chat_id": 0})()

            asyncio.run(model.handle(args, container, ui))

            self.assertEqual(len(ui.blocks), 1)
            self.assertIn("Claude Sonnet 4", ui.blocks[0][1])
            self.assertIn("Claude 3.7 Sonnet", ui.blocks[0][1])


if __name__ == "__main__":
    unittest.main()
