import unittest

from core.metrics import MetricsCollector


class MetricsCollectorTests(unittest.TestCase):
    def test_records_tasks_and_renders_prometheus(self):
        metrics = MetricsCollector()

        metrics.record_task("qwen", 0, 1200)
        metrics.record_task("qwen", 1, 900)
        metrics.record_task("codex", 0, 500)
        metrics.record_orchestrated_run("success")
        metrics.record_orchestrated_run("failed")

        payload = metrics.render_prometheus(["provider health", "qwen ok"])

        self.assertIn("forge_tasks_total 3", payload)
        self.assertIn('forge_provider_tasks_total{provider="qwen"} 2', payload)
        self.assertIn('forge_provider_failures_total{provider="qwen"} 1', payload)
        self.assertIn("forge_orchestrated_runs_total 2", payload)
        self.assertIn("# qwen ok", payload)


if __name__ == "__main__":
    unittest.main()
