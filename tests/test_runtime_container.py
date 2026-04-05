import tempfile
import unittest
from pathlib import Path

from runtime import RuntimeContainer


class RuntimeContainerTests(unittest.TestCase):
    def test_build_planner_and_session_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            container = RuntimeContainer(sessions_root=Path(tmpdir))
            session = container.get_session(100)
            planner = container.build_planner(session)
            plan = planner.build_plan("Build GTK UI with Rust backend and Python parser")

            self.assertEqual(session.chat_id, 100)
            self.assertGreaterEqual(len(plan.subtasks), 2)
            self.assertIn("qwen", container.provider_paths)


if __name__ == "__main__":
    unittest.main()
