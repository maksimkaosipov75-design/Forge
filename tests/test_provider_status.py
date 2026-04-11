import unittest

from core.provider_status import ProviderHealth, classify_failure_text


class ProviderStatusTests(unittest.TestCase):
    def test_classifies_limit_with_retry_time(self):
        failure = classify_failure_text("You hit your limit. Agent will be available at 21:34.")

        self.assertIsNotNone(failure)
        self.assertEqual(failure.kind, "limit")
        self.assertEqual(failure.retry_at, "21:34")

    def test_classifies_network_issue(self):
        failure = classify_failure_text("Connection error. fetch failed while reconnecting.")

        self.assertIsNotNone(failure)
        self.assertEqual(failure.kind, "network")

    def test_provider_health_registers_failure(self):
        health = ProviderHealth(provider="claude")
        failure = classify_failure_text("Rate limit hit. Try again at 18:20.")

        health.register_failure(failure)

        self.assertFalse(health.available)
        self.assertEqual(health.last_limit_reset_at, "18:20")
        self.assertEqual(health.last_failure.kind, "limit")


if __name__ == "__main__":
    unittest.main()
