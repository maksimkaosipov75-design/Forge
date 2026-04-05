import unittest

from orchestrator import RuleBasedOrchestrator


class RuleBasedOrchestratorTests(unittest.TestCase):
    def test_builds_multi_subtask_plan_for_mixed_stack_prompt(self):
        planner = RuleBasedOrchestrator(["qwen", "codex", "claude"])

        plan = planner.build_plan(
            "Build a desktop Linux app with a Python parser, Rust backend, GTK4 libadwaita UI, and custom CSS."
        )

        self.assertEqual(plan.complexity, "complex")
        self.assertEqual(len(plan.subtasks), 3)
        self.assertEqual(plan.subtasks[0].suggested_provider, "qwen")
        self.assertEqual(plan.subtasks[1].suggested_provider, "codex")
        self.assertEqual(plan.subtasks[2].suggested_provider, "claude")

    def test_falls_back_when_preferred_provider_is_unavailable(self):
        planner = RuleBasedOrchestrator(["qwen", "codex"])

        plan = planner.build_plan("Polish the GTK UI and CSS theme for the app.")

        self.assertEqual(len(plan.subtasks), 1)
        self.assertEqual(plan.subtasks[0].suggested_provider, "qwen")


if __name__ == "__main__":
    unittest.main()
