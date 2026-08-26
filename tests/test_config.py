import os
import unittest
from unittest.mock import patch

from core.config import Settings


class ConfigTests(unittest.TestCase):
    def test_status_http_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
        self.assertFalse(settings.ENABLE_STATUS_HTTP)

    def test_status_http_can_be_enabled_explicitly(self):
        with patch.dict(os.environ, {"ENABLE_STATUS_HTTP": "true"}, clear=True):
            settings = Settings()
        self.assertTrue(settings.ENABLE_STATUS_HTTP)

    def test_invalid_integer_env_uses_default(self):
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_HTTP_TIMEOUT": "oops",
                "STATUS_HTTP_PORT": "not-a-port",
            },
            clear=False,
        ):
            settings = Settings()
        self.assertEqual(settings.OPENROUTER_HTTP_TIMEOUT, 300)
        self.assertEqual(settings.STATUS_HTTP_PORT, 8089)

    def test_minimum_integer_env_is_enforced(self):
        with patch.dict(
            os.environ,
            {
                "RATE_LIMIT_MAX_REQUESTS": "0",
                "MAX_PROMPT_LENGTH": "10",
            },
            clear=False,
        ):
            settings = Settings()
        self.assertEqual(settings.RATE_LIMIT_MAX_REQUESTS, 1)
        self.assertEqual(settings.MAX_PROMPT_LENGTH, 256)

    def test_claude_bypass_permissions_defaults_to_false(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
        self.assertFalse(settings.CLAUDE_BYPASS_PERMISSIONS)

    def test_local_streaming_defaults_to_false(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()
        self.assertFalse(settings.LOCAL_LLM_ENABLE_STREAMING)


if __name__ == "__main__":
    unittest.main()
