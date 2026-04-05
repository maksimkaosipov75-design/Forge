import tempfile
import unittest
from pathlib import Path

from file_manager import FileManager
from orchestrator import OrchestrationPlan, PlannedSubtask
from session_store import SessionStore
from task_models import ChatSession, TaskResult, TaskRun


class SessionStoreTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = SessionStore(root)
            session = ChatSession(chat_id=42, file_mgr=FileManager(projects_file=str(root / "projects.json")))
            session.current_provider = "claude"
            session.last_task_result = TaskResult(prompt="hello", provider="qwen", answer_text="world")
            session.last_task_run = TaskRun.from_task_result(session.last_task_result)
            session.run_history = [session.last_task_run]
            session.history = [session.last_task_result]
            session.last_plan = OrchestrationPlan(
                prompt="build app",
                complexity="medium",
                strategy="split by specialty",
                subtasks=[
                    PlannedSubtask(
                        subtask_id="ui",
                        title="UI",
                        description="Build UI",
                        task_kind="ui_surface",
                        suggested_provider="claude",
                        reason="UI fit",
                    )
                ],
            )

            store.save(session)

            restored = ChatSession(chat_id=42, file_mgr=FileManager(projects_file=str(root / "projects.json")))
            store.load(restored)

            self.assertEqual(restored.current_provider, "claude")
            self.assertEqual(restored.last_task_result.answer_text, "world")
            self.assertEqual(len(restored.run_history), 1)
            self.assertEqual(restored.last_plan.subtasks[0].suggested_provider, "claude")

    def test_write_run_artifact_creates_markdown_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = SessionStore(root)
            session = ChatSession(chat_id=7, file_mgr=FileManager(projects_file=str(root / "projects.json")))
            run = TaskRun.from_task_result(TaskResult(prompt="hello", provider="qwen", answer_text="world"))

            artifact_path = Path(store.write_run_artifact(session, run))

            self.assertTrue(artifact_path.exists())
            content = artifact_path.read_text(encoding="utf-8")
            self.assertIn("# Run", content)
            self.assertIn("Final Answer", content)


if __name__ == "__main__":
    unittest.main()
