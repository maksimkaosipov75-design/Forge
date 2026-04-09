import tempfile
import unittest
from pathlib import Path

from credential_store import CredentialStore


class CredentialStoreTests(unittest.TestCase):
    def test_roundtrip_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")

            store.set_api_key("openrouter", "secret-123")

            self.assertTrue(store.has_api_key("openrouter"))
            self.assertEqual(store.get_api_key("openrouter"), "secret-123")
            self.assertEqual(store.configured_providers(), ["openrouter"])

    def test_delete_api_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(Path(tmpdir) / "secrets.json")
            store.set_api_key("openrouter", "secret-123")

            store.delete_api_key("openrouter")

            self.assertFalse(store.has_api_key("openrouter"))
            self.assertEqual(store.get_api_key("openrouter"), "")


if __name__ == "__main__":
    unittest.main()
