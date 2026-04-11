import json
import tempfile
import unittest
from pathlib import Path

from core.openrouter_catalog import OpenRouterModelCatalog


class OpenRouterCatalogTests(unittest.TestCase):
    def test_featured_models_support_alias_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog = OpenRouterModelCatalog(
                cache_path=Path(tmpdir) / "openrouter_models.json",
                base_url="https://openrouter.ai/api/v1",
                ttl_seconds=3600,
            )

            result = catalog.resolve_model("qwen3")

            self.assertEqual(result.status, "exact")
            self.assertEqual(result.model_name, "qwen/qwen3-coder:free")

    def test_cached_models_can_be_searched_by_family_name(self):
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
                                "aliases": ["claude sonnet 4", "sonnet", "claude sonnet"],
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
            catalog = OpenRouterModelCatalog(
                cache_path=cache_path,
                base_url="https://openrouter.ai/api/v1",
                ttl_seconds=3600,
            )

            result = catalog.resolve_model("sonnet")

            self.assertEqual(result.status, "exact")
            self.assertEqual(result.model_name, "anthropic/claude-sonnet-4")

    def test_ambiguous_search_returns_matches(self):
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
            catalog = OpenRouterModelCatalog(
                cache_path=cache_path,
                base_url="https://openrouter.ai/api/v1",
                ttl_seconds=3600,
            )

            result = catalog.resolve_model("sonnet")

            self.assertEqual(result.status, "ambiguous")
            self.assertGreaterEqual(len(result.matches), 2)


if __name__ == "__main__":
    unittest.main()
