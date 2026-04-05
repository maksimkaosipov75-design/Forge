import unittest

from orchestrator import OrchestrationPlan, PlannedSubtask
from runtime.orchestrator_service import OrchestratorService
from task_models import SubtaskRun, TaskRun


class OrchestratorServiceTests(unittest.TestCase):
    def test_find_retry_start_index_prefers_failed_subtask(self):
        run = TaskRun(
            run_id="run-1",
            prompt="build app",
            mode="orchestrated",
            status="partial",
            subtasks=[
                SubtaskRun(subtask_id="a", title="A", provider="qwen", status="success"),
                SubtaskRun(subtask_id="b", title="B", provider="codex", status="failed"),
            ],
        )

        self.assertEqual(OrchestratorService.find_retry_start_index(run), 1)

    def test_find_retry_start_index_can_retry_synthesis(self):
        run = TaskRun(
            run_id="run-1",
            prompt="build app",
            mode="orchestrated",
            status="partial",
            subtasks=[SubtaskRun(subtask_id="a", title="A", provider="qwen", status="success")],
        )

        self.assertEqual(OrchestratorService.find_retry_start_index(run), 1)

    def test_build_subtask_prompt_includes_previous_results(self):
        plan = OrchestrationPlan(
            prompt="build app",
            complexity="medium",
            strategy="split",
            subtasks=[
                PlannedSubtask(
                    subtask_id="ui",
                    title="UI",
                    description="Build UI",
                    task_kind="ui_surface",
                    suggested_provider="claude",
                    reason="UI fit",
                    depends_on=["backend"],
                )
            ],
        )

        previous = [
            type("R", (), {"provider": "codex", "prompt": "backend", "answer_text": "done"})()
        ]
        prompt = OrchestratorService.build_subtask_prompt(plan, plan.subtasks[0], previous)

        self.assertIn("Original task", prompt)
        self.assertIn("Previous subtask outputs", prompt)
        self.assertIn("codex", prompt)


if __name__ == "__main__":
    unittest.main()
