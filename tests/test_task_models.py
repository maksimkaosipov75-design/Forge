import unittest

from task_models import TaskResult, TaskRun


class TaskResultTests(unittest.TestCase):
    def test_touched_files_deduplicates(self):
        result = TaskResult(
            new_files=["a.py", "b.py"],
            changed_files=["b.py", "c.py"],
        )

        self.assertEqual(result.touched_files, ["a.py", "b.py", "c.py"])

    def test_has_details_checks_meaningful_content(self):
        empty = TaskResult()
        filled = TaskResult(prompt="hello")

        self.assertFalse(empty.has_details)
        self.assertTrue(filled.has_details)

    def test_provider_defaults_to_qwen(self):
        result = TaskResult()

        self.assertEqual(result.provider, "qwen")

    def test_duration_and_status_helpers(self):
        result = TaskResult(exit_code=7, duration_ms=6500)

        self.assertEqual(result.short_status, "⚠️ 7")
        self.assertEqual(result.duration_text, "6.5с")

    def test_finished_or_started_at_prefers_finished_at(self):
        result = TaskResult(started_at="2026-04-04T10:00:00+00:00", finished_at="2026-04-04T10:01:00+00:00")

        self.assertEqual(result.finished_or_started_at, "2026-04-04T10:01:00+00:00")

    def test_task_run_from_task_result_preserves_files_and_status(self):
        result = TaskResult(
            provider="codex",
            prompt="Refactor backend",
            new_files=["src/new.rs"],
            changed_files=["src/lib.rs"],
            exit_code=0,
            answer_text="Done",
        )

        run = TaskRun.from_task_result(result)

        self.assertEqual(run.status, "success")
        self.assertEqual(run.provider_summary, "codex")
        self.assertEqual(run.new_files, ["src/new.rs"])
        self.assertEqual(run.changed_files, ["src/lib.rs"])
        self.assertEqual(len(run.subtasks), 1)

    def test_task_run_touched_files_are_deduplicated(self):
        result = TaskResult(
            new_files=["a.py", "b.py"],
            changed_files=["b.py", "c.py"],
        )

        run = TaskRun.from_task_result(result)

        self.assertEqual(run.touched_files, ["a.py", "b.py", "c.py"])

    def test_task_run_supports_synthesis_and_handoff_fields(self):
        run = TaskRun(
            run_id="run-1",
            prompt="Build app",
            synthesis_provider="claude",
            synthesis_answer="Final summary",
            review_provider="codex",
            review_answer="Looks mostly good",
            handoff_artifacts=["backend done", "ui done"],
        )

        self.assertEqual(run.synthesis_provider, "claude")
        self.assertEqual(run.synthesis_answer, "Final summary")
        self.assertEqual(run.review_provider, "codex")
        self.assertEqual(run.review_answer, "Looks mostly good")
        self.assertEqual(run.handoff_artifacts, ["backend done", "ui done"])


if __name__ == "__main__":
    unittest.main()
