import unittest

from core.providers import (
    get_provider_definition,
    is_api_provider,
    is_cli_provider,
    list_provider_models,
    provider_default_model,
    provider_transport,
)


class ProviderDefinitionsTests(unittest.TestCase):
    def test_openrouter_is_api_provider(self):
        self.assertTrue(is_api_provider("openrouter"))
        self.assertFalse(is_cli_provider("openrouter"))
        self.assertEqual(provider_transport("openrouter"), "api")

    def test_openrouter_has_default_model_and_catalog(self):
        definition = get_provider_definition("openrouter")

        self.assertEqual(provider_default_model("openrouter"), "qwen/qwen3-coder:free")
        self.assertEqual(definition.transport, "api")
        self.assertGreaterEqual(len(list_provider_models("openrouter")), 3)

    def test_cli_providers_remain_cli(self):
        self.assertTrue(is_cli_provider("qwen"))
        self.assertTrue(is_cli_provider("codex"))
        self.assertTrue(is_cli_provider("claude"))


if __name__ == "__main__":
    unittest.main()
