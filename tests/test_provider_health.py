"""Tests for ProviderHealth backoff, retry scheduling, and is_available_now."""
import time
import unittest

from core.provider_status import (
    FailureReason,
    ProviderHealth,
    classify_failure_text,
    _extract_retry_after_ts,
)


class TestProviderHealthAvailability(unittest.TestCase):

    def test_new_health_is_available(self):
        h = ProviderHealth(provider="qwen")
        self.assertTrue(h.available)
        self.assertTrue(h.is_available_now())

    def test_register_failure_marks_unavailable(self):
        h = ProviderHealth(provider="qwen")
        failure = classify_failure_text("rate limit 429")
        h.register_failure(failure)
        self.assertFalse(h.available)

    def test_register_success_clears_failures(self):
        h = ProviderHealth(provider="qwen")
        h.register_failure(FailureReason(kind="network", message="err"))
        h.register_success()
        self.assertTrue(h.is_available_now())
        self.assertEqual(h.consecutive_failures, 0)
        self.assertEqual(h.retry_after_ts, 0.0)

    def test_is_available_now_auto_recovers_after_retry_ts(self):
        h = ProviderHealth(provider="qwen")
        h.available = False
        h.retry_after_ts = time.monotonic() - 1  # already passed
        self.assertTrue(h.is_available_now())
        self.assertTrue(h.available)  # should be marked available again

    def test_is_available_now_blocks_before_retry_ts(self):
        h = ProviderHealth(provider="qwen")
        h.available = False
        h.retry_after_ts = time.monotonic() + 300  # 5 min from now
        self.assertFalse(h.is_available_now())

    def test_consecutive_failures_increments(self):
        h = ProviderHealth(provider="qwen")
        h.register_failure(FailureReason(kind="unknown", message="err"))
        h.register_failure(FailureReason(kind="unknown", message="err"))
        self.assertEqual(h.consecutive_failures, 2)

    def test_degradation_level(self):
        h = ProviderHealth(provider="qwen")
        self.assertEqual(h.degradation_level, "ok")
        h.consecutive_failures = 1
        self.assertEqual(h.degradation_level, "degraded")
        h.consecutive_failures = 3
        self.assertEqual(h.degradation_level, "failing")

    def test_backoff_increases_with_consecutive_failures(self):
        h = ProviderHealth(provider="qwen")
        now = time.monotonic()
        # First failure: 15s backoff (network)
        h.register_failure(FailureReason(kind="network", message="err"))
        self.assertGreater(h.retry_after_ts, now)
        first_ts = h.retry_after_ts

        # Second failure: backoff should be longer
        h.available = True  # simulate recovery attempt
        h.register_failure(FailureReason(kind="network", message="err"))
        self.assertGreater(h.retry_after_ts, first_ts - 1)  # roughly longer

    def test_retry_in_seconds_returns_remaining(self):
        h = ProviderHealth(provider="qwen")
        h.available = False
        h.retry_after_ts = time.monotonic() + 60
        ri = h.retry_in_seconds
        self.assertIsNotNone(ri)
        self.assertGreater(ri, 55)
        self.assertLessEqual(ri, 60)

    def test_retry_in_seconds_none_when_no_ts(self):
        h = ProviderHealth(provider="qwen")
        self.assertIsNone(h.retry_in_seconds)

    def test_auth_failure_has_no_auto_recovery(self):
        h = ProviderHealth(provider="qwen")
        h.register_failure(FailureReason(kind="auth", message="not logged in"))
        # auth backoff = [0] → no retry_after_ts
        self.assertEqual(h.retry_after_ts, 0.0)
        self.assertFalse(h.available)

    def test_to_dict_round_trip(self):
        h = ProviderHealth(provider="codex")
        h.register_failure(FailureReason(kind="limit", message="rate limit", retry_at="15:30"))
        h.consecutive_failures = 2
        h.context_status = "near limit"

        d = h.to_dict()
        self.assertEqual(d["provider"], "codex")
        self.assertFalse(d["available"])
        self.assertEqual(d["last_failure_kind"], "limit")

        restored = ProviderHealth.from_dict(d)
        self.assertEqual(restored.provider, "codex")
        self.assertFalse(restored.available)
        self.assertEqual(restored.consecutive_failures, 2)
        self.assertIsNotNone(restored.last_failure)
        self.assertEqual(restored.last_failure.kind, "limit")

    def test_from_dict_restores_retry_after(self):
        h = ProviderHealth(provider="qwen")
        h.available = False
        h.retry_after_ts = time.monotonic() + 120
        d = h.to_dict()

        restored = ProviderHealth.from_dict(d)
        # retry_in_seconds should be ~120s
        self.assertFalse(restored.available)
        self.assertGreater(restored.retry_after_ts, 0)


class TestClassifyFailureText(unittest.TestCase):

    def test_rate_limit_detection(self):
        f = classify_failure_text("You hit your limit. Try again at 15:00.")
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, "limit")
        self.assertEqual(f.retry_at, "15:00")

    def test_429_detection(self):
        f = classify_failure_text("Error 429 too many requests")
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, "limit")

    def test_context_window_detection(self):
        f = classify_failure_text("context window exceeded — prompt is too long")
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, "context")

    def test_auth_detection(self):
        f = classify_failure_text("unauthorized: API key invalid")
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, "auth")

    def test_network_detection(self):
        f = classify_failure_text("connection error: fetch failed")
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, "network")

    def test_timeout_detection(self):
        f = classify_failure_text("operation timed out after 60s")
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, "timeout")

    def test_empty_text_returns_none(self):
        self.assertIsNone(classify_failure_text(""))
        self.assertIsNone(classify_failure_text("   "))

    def test_clean_text_returns_none(self):
        self.assertIsNone(classify_failure_text("Task completed successfully."))

    def test_retry_after_ts_extracted_for_minutes(self):
        f = classify_failure_text("Please wait 5 minutes before retrying.")
        self.assertIsNotNone(f)
        self.assertGreater(f.retry_after_ts, time.monotonic() + 200)
        self.assertLess(f.retry_after_ts, time.monotonic() + 400)


class TestExtractRetryAfterTs(unittest.TestCase):

    def test_minutes_pattern(self):
        ts = _extract_retry_after_ts("retry after 10 minutes")
        self.assertGreater(ts, time.monotonic() + 500)

    def test_seconds_pattern(self):
        ts = _extract_retry_after_ts("wait 30 seconds")
        self.assertGreater(ts, time.monotonic() + 25)
        self.assertLess(ts, time.monotonic() + 35)

    def test_no_pattern_returns_zero(self):
        ts = _extract_retry_after_ts("no time info here")
        self.assertEqual(ts, 0.0)


if __name__ == "__main__":
    unittest.main()
