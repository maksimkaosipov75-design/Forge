import unittest

from core.providers import (
    get_provider_definition,
    is_api_provider,
    is_cli_provider,
    list_provider_models,
    provider_default_model,
    provider_supports_thinking,
    provider_transport,
    resolve_provider_model_definition,
)


class ProviderDefinitionsTests(unittest.TestCase):
    def test_openrouter_is_api_provider(self):
        self.assertTrue(is_api_provider("openrouter"))
        self.assertFalse(is_cli_provider("openrouter"))
        self.assertEqual(provider_transport("openrouter"), "api")

    def test_local_is_api_provider(self):
        self.assertTrue(is_api_provider("local"))
        self.assertFalse(is_cli_provider("local"))
        self.assertEqual(provider_transport("local"), "api")

    def test_openrouter_has_default_model_and_catalog(self):
        definition = get_provider_definition("openrouter")

        self.assertEqual(provider_default_model("openrouter"), "qwen/qwen3-coder:free")
        self.assertEqual(definition.transport, "api")
        self.assertGreaterEqual(len(list_provider_models("openrouter")), 3)

    def test_local_has_default_model_and_catalog(self):
        definition = get_provider_definition("local")

        self.assertEqual(provider_default_model("local"), "qwen2.5-coder:7b")
        self.assertEqual(definition.transport, "api")
        self.assertEqual(definition.accent_color, "red")
        self.assertIn("tool_use", definition.capabilities)
        self.assertGreaterEqual(len(list_provider_models("local")), 4)

    def test_cli_providers_remain_cli(self):
        self.assertTrue(is_cli_provider("qwen"))
        self.assertTrue(is_cli_provider("codex"))
        self.assertTrue(is_cli_provider("claude"))

    def test_cli_providers_have_curated_model_catalogs(self):
        self.assertGreaterEqual(len(list_provider_models("qwen")), 4)
        self.assertGreaterEqual(len(list_provider_models("codex")), 4)
        self.assertGreaterEqual(len(list_provider_models("claude")), 4)

        self.assertEqual(resolve_provider_model_definition("claude", "sonnet").name, "claude-sonnet-4-6")
        self.assertEqual(resolve_provider_model_definition("codex", "mini").name, "gpt-5.4-mini")

    def test_thinking_support_is_exposed_only_where_configurable(self):
        self.assertTrue(provider_supports_thinking("openrouter", "qwen/qwen3-coder:free"))
        self.assertTrue(provider_supports_thinking("claude", "claude-sonnet-4-6"))
        self.assertFalse(provider_supports_thinking("qwen", "qwen3-coder-plus"))
        self.assertFalse(provider_supports_thinking("codex", "gpt-5.3-codex"))


if __name__ == "__main__":
    unittest.main()
