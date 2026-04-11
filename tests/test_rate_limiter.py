import unittest

from core.rate_limiter import RateLimiter


class RateLimiterTests(unittest.TestCase):
    def test_allows_requests_within_window(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60)

        allowed1, retry1 = limiter.check("user-1")
        allowed2, retry2 = limiter.check("user-1")

        self.assertTrue(allowed1)
        self.assertEqual(retry1, 0)
        self.assertTrue(allowed2)
        self.assertEqual(retry2, 0)

    def test_blocks_after_limit(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)

        limiter.check("user-1")
        allowed, retry_after = limiter.check("user-1")

        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)


if __name__ == "__main__":
    unittest.main()
