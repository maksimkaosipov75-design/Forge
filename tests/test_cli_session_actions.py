import tempfile
import unittest
from pathlib import Path

from cli.session_actions import build_commit_message, compact_session, render_todos_lines
from file_manager import FileManager
from task_models import ChatSession, TaskResult, TaskRun


class CliSessionActionsTests(unittest.TestCase):
    def test_compact_session_keeps_last_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = ChatSession(chat_id=1, file_mgr=FileManager(projects_file=str(Path(tmpdir) / "projects.json")))
            session.history = [
                TaskResult(prompt=f"task {idx}", answer_text=f"answer {idx}")
                for idx in range(5)
            ]
            session.run_history = [TaskRun.from_task_result(item) for item in session.history]

            message = compact_session(session, keep=2)

            self.assertIn("last 2", message)
            self.assertEqual(len(session.history), 2)
            self.assertEqual(len(session.run_history), 2)

    def test_render_todos_lines_extracts_checklist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = ChatSession(chat_id=1, file_mgr=FileManager(projects_file=str(Path(tmpdir) / "projects.json")))
            session.last_task_result = TaskResult(answer_text="- [ ] first\n- [x] second")

            lines = render_todos_lines(session)

            self.assertTrue(any("first" in line for line in lines))
            self.assertTrue(any("second" in line for line in lines))

    def test_build_commit_message_falls_back_to_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = ChatSession(chat_id=1, file_mgr=FileManager(projects_file=str(Path(tmpdir) / "projects.json")))
            session.last_task_result = TaskResult(prompt="Implement better CLI help output")

            message = build_commit_message(session)

            self.assertIn("Implement better CLI help output", message)


if __name__ == "__main__":
    unittest.main()
