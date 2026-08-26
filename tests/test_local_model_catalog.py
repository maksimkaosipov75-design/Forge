import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.local_model_catalog import LocalModelCatalog


class _FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class _FakeStreamingResponse:
    def __init__(self, payloads):
        self.lines = [(json.dumps(payload) + "\n").encode("utf-8") for payload in payloads]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        return iter(self.lines)


class LocalModelCatalogTests(unittest.TestCase):
    def test_lists_installed_openai_models_plus_pull_candidates(self):
        catalog = LocalModelCatalog(lambda: "http://127.0.0.1:11434/v1")

        def fake_urlopen(req, timeout=0):
            url = req.full_url
            if url.endswith("/v1/models"):
                return _FakeResponse({"data": [{"id": "local-installed:latest"}]})
            if url.endswith("/api/tags"):
                return _FakeResponse({"models": []})
            return _FakeResponse({})

        with patch("core.local_model_catalog.request.urlopen", fake_urlopen):
            models = catalog.list_models(refresh=True)

        names = [item.name for item in models]
        installed = next(item for item in models if item.name == "local-installed:latest")
        self.assertIn("local-installed:latest", names)
        self.assertIn("qwen2.5-coder:7b", names)
        self.assertIn("installed", installed.description)
        self.assertIn("tools unknown", installed.description)

    def test_resolves_curated_local_alias(self):
        catalog = LocalModelCatalog(lambda: "http://127.0.0.1:11434/v1")

        with patch("core.local_model_catalog.request.urlopen", return_value=_FakeResponse({})):
            result = catalog.resolve_model("devstral")

        self.assertEqual(result.status, "exact")
        self.assertEqual(result.model_name, "devstral:latest")

    def test_lists_tool_capable_local_models_separately(self):
        catalog = LocalModelCatalog(lambda: "http://127.0.0.1:11434/v1")

        with patch("core.local_model_catalog.request.urlopen", return_value=_FakeResponse({})):
            models = catalog.list_models(refresh=True, tools_only=True)

        names = [item.name for item in models]
        self.assertIn("qwen2.5-coder:7b", names)
        self.assertIn("devstral:latest", names)
        self.assertNotIn("llama3.1:8b", names)
        self.assertTrue(all("tool_use" in item.capabilities for item in models))

    def test_require_tools_rejects_chat_only_exact_match(self):
        catalog = LocalModelCatalog(lambda: "http://127.0.0.1:11434/v1")

        with patch("core.local_model_catalog.request.urlopen", return_value=_FakeResponse({})):
            result = catalog.resolve_model("llama3.1:8b", require_tools=True)

        self.assertEqual(result.status, "missing")
        self.assertIn("not marked as tool-capable", result.message)

    def test_exact_unknown_local_model_is_allowed(self):
        catalog = LocalModelCatalog(lambda: "http://127.0.0.1:11434/v1")

        with patch("core.local_model_catalog.request.urlopen", return_value=_FakeResponse({})):
            result = catalog.resolve_model("my-model:latest")

        self.assertEqual(result.status, "raw")
        self.assertEqual(result.model_name, "my-model:latest")

    def test_searches_huggingface_models_as_install_candidates(self):
        catalog = LocalModelCatalog(lambda: "http://127.0.0.1:11434/v1")

        def fake_urlopen(req, timeout=0):
            url = req.full_url
            if url.startswith("https://huggingface.co/api/models?"):
                self.assertIn("filter=gguf", url)
                return _FakeResponse([
                    {
                        "modelId": "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
                        "downloads": 1234,
                        "tags": ["text-generation", "gguf"],
                    }
                ])
            return _FakeResponse({})

        with patch("core.local_model_catalog.request.urlopen", fake_urlopen):
            models = catalog.search_models("qwen coder gguf")

        self.assertEqual(models[0].name, "hf.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF")
        self.assertIn("not installed", models[0].description)
        self.assertIn("Hugging Face", models[0].description)

    def test_parses_ollama_pull_progress_line(self):
        catalog = LocalModelCatalog(lambda: "http://127.0.0.1:11434/v1")
        events = []

        catalog._handle_pull_output_line(
            "pulling abcd1234 42% ▕████▱▱▱▱▱▱▏ 1.2 GB/2.8 GB",
            "qwen2.5-coder:7b",
            events.append,
        )

        self.assertEqual(events[0].model_name, "qwen2.5-coder:7b")
        self.assertEqual(events[0].percent, 42)
        self.assertFalse(events[0].installed)

    def test_http_pull_progress_uses_completed_and_total(self):
        catalog = LocalModelCatalog(lambda: "http://127.0.0.1:11434/v1")
        events = []

        def fake_urlopen(req, timeout=0):
            return _FakeStreamingResponse([
                {"status": "pulling manifest"},
                {"status": "pulling layer", "completed": 25, "total": 100},
                {"status": "success"},
            ])

        with patch("core.local_model_catalog.request.urlopen", fake_urlopen):
            result = catalog._pull_model_http("qwen2.5-coder:7b", events.append)

        self.assertTrue(result.ok)
        self.assertEqual(events[1].percent, 25)
        self.assertTrue(events[-1].installed)

    def test_http_pull_reports_manifest_error_with_huggingface_hint(self):
        catalog = LocalModelCatalog(lambda: "http://127.0.0.1:11434/v1")

        def fake_urlopen(req, timeout=0):
            return _FakeStreamingResponse([
                {"status": "pulling manifest"},
                {"error": "pull model manifest: file does not exist"},
            ])

        with patch("core.local_model_catalog.request.urlopen", fake_urlopen):
            result = catalog._pull_model_http("hf.co/example/not-gguf")

        self.assertFalse(result.ok)
        self.assertIn("pull model manifest", result.message)
        self.assertIn("GGUF", result.message)


if __name__ == "__main__":
    unittest.main()
