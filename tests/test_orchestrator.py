import unittest

from core.orchestrator import RuleBasedOrchestrator


class RuleBasedOrchestratorTests(unittest.TestCase):
    def test_builds_multi_subtask_plan_for_mixed_stack_prompt(self):
        planner = RuleBasedOrchestrator(["qwen", "codex", "claude"])

        plan = planner.build_plan(
            "Build a desktop Linux app with a Python parser, Rust backend, GTK4 libadwaita UI, and custom CSS."
        )

        self.assertEqual(plan.complexity, "complex")
        self.assertGreaterEqual(len(plan.subtasks), 5)
        self.assertEqual(plan.subtasks[0].subtask_id, "project-brief")
        self.assertEqual(plan.subtasks[0].suggested_provider, "qwen")
        provider_by_id = {item.subtask_id: item.suggested_provider for item in plan.subtasks}
        self.assertEqual(provider_by_id["python-data"], "qwen")
        self.assertEqual(provider_by_id["backend-core"], "codex")
        self.assertEqual(provider_by_id["ui-surface"], "claude")
        integration = next(item for item in plan.subtasks if item.subtask_id == "integration")
        self.assertIn("python-data", integration.depends_on)
        self.assertIn("backend-core", integration.depends_on)
        self.assertIn("ui-surface", integration.depends_on)

    def test_falls_back_when_preferred_provider_is_unavailable(self):
        planner = RuleBasedOrchestrator(["qwen", "codex"])

        plan = planner.build_plan("Polish the GTK UI and CSS theme for the app.")

        self.assertEqual(len(plan.subtasks), 1)
        self.assertEqual(plan.subtasks[0].suggested_provider, "qwen")


if __name__ == "__main__":
    unittest.main()
