import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from core.credential_store import CredentialStore
from core.local_model_catalog import LocalModelPullResult
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

    def test_model_command_pulls_and_selects_local_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            container.pull_local_model = AsyncMock(  # type: ignore[method-assign]
                return_value=LocalModelPullResult(True, "qwen2.5-coder:7b", "ok")
            )
            ui = _DummyUi()
            args = type("Args", (), {
                "provider": "local",
                "model": "pull",
                "extra": ["qwen2.5-coder:7b"],
                "chat_id": 0,
            })()

            asyncio.run(model.handle(args, container, ui))

            session = container.get_session(0)
            self.assertEqual(session.provider_models["local"], "qwen2.5-coder:7b")
            self.assertIn("Downloaded and selected local model", ui.notices[-1][0])

    def test_model_command_installs_uninstalled_local_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            container.local_model_is_installed = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
            container.pull_local_model = AsyncMock(  # type: ignore[method-assign]
                return_value=LocalModelPullResult(True, "qwen2.5-coder:7b", "ok")
            )
            ui = _DummyUi()
            args = type("Args", (), {
                "provider": "local",
                "model": "qwen2.5-coder:7b",
                "extra": [],
                "chat_id": 0,
            })()

            asyncio.run(model.handle(args, container, ui))

            session = container.get_session(0)
            self.assertEqual(session.provider_models["local"], "qwen2.5-coder:7b")
            self.assertTrue(any("not installed" in notice[0] for notice in ui.notices))

    def test_model_command_lists_local_tool_models(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            ui = _DummyUi()
            args = type("Args", (), {
                "provider": "local",
                "model": "tools",
                "extra": [],
                "chat_id": 0,
            })()

            asyncio.run(model.handle(args, container, ui))

            self.assertEqual(len(ui.blocks), 1)
            self.assertIn("Tool-capable local models", ui.blocks[0][1])
            self.assertIn("qwen2.5-coder:7b", ui.blocks[0][1])
            self.assertNotIn("llama3.1:8b", ui.blocks[0][1])

    def test_model_command_rejects_chat_only_model_when_tools_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            container = RuntimeContainer(sessions_root=Path(tmpdir), credential_store=store)
            ui = _DummyUi()
            args = type("Args", (), {
                "provider": "local",
                "model": "tools",
                "extra": ["llama3.1:8b"],
                "chat_id": 0,
            })()

            asyncio.run(model.handle(args, container, ui))

            self.assertTrue(ui.notices)
            self.assertIn("not marked as tool-capable", ui.notices[-1][0])


if __name__ == "__main__":
    unittest.main()
