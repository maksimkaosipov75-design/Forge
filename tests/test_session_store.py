import json
import tempfile
import unittest
from pathlib import Path

from file_manager import FileManager
from orchestrator import OrchestrationPlan, PlannedSubtask
from session_store import SessionStore
from task_models import ChatSession, SubtaskRun, TaskResult, TaskRun


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

            self.assertTrue((root / "session_store.sqlite3").exists())
            self.assertEqual(restored.current_provider, "claude")
            self.assertEqual(restored.last_task_result.answer_text, "world")
            self.assertEqual(len(restored.run_history), 1)
            self.assertEqual(restored.last_plan.subtasks[0].suggested_provider, "claude")

    def test_load_migrates_legacy_json_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_payload = {
                "current_provider": "codex",
                "provider_models": {"codex": "o3"},
                "last_task_result": {
                    "provider": "codex",
                    "prompt": "legacy prompt",
                    "answer_text": "legacy answer",
                    "new_files": [],
                    "changed_files": [],
                    "exit_code": 0,
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:00:01+00:00",
                    "duration_ms": 1000,
                    "error_text": "",
                },
                "history": [],
                "last_task_run": None,
                "run_history": [],
                "last_plan": None,
                "provider_stats": {},
                "provider_health": {},
            }
            (root / "chat_55_state.json").write_text(json.dumps(legacy_payload), encoding="utf-8")

            store = SessionStore(root)
            restored = ChatSession(chat_id=55, file_mgr=FileManager(projects_file=str(root / "projects.json")))
            store.load(restored)

            self.assertEqual(restored.current_provider, "codex")
            self.assertEqual(restored.last_task_result.answer_text, "legacy answer")
            self.assertTrue((root / "session_store.sqlite3").exists())

    def test_write_run_artifact_creates_markdown_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = SessionStore(root)
            session = ChatSession(chat_id=7, file_mgr=FileManager(projects_file=str(root / "projects.json")))
            run = TaskRun.from_task_result(
                TaskResult(
                    prompt="hello",
                    provider="qwen",
                    model_name="qwen3-coder-plus",
                    transport="cli",
                    input_tokens=12,
                    output_tokens=34,
                    total_input_tokens=12,
                    total_output_tokens=34,
                    answer_text="world",
                )
            )

            artifact_path = Path(store.write_run_artifact(session, run))

            self.assertTrue(artifact_path.exists())
            content = artifact_path.read_text(encoding="utf-8")
            self.assertIn("# Run", content)
            self.assertIn("Final Answer", content)
            self.assertIn("Model summary", content)
            self.assertIn("Transport summary", content)
            self.assertIn("Tokens: 12 in / 34 out", content)

    def test_save_and_load_preserves_structured_handoffs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = SessionStore(root)
            session = ChatSession(chat_id=7, file_mgr=FileManager(projects_file=str(root / "projects.json")))
            session.last_task_run = TaskRun(
                run_id="run-1",
                prompt="build app",
                handoff_artifacts=["plain summary"],
                handoff_records=[
                    {
                        "subtask_id": "backend",
                        "title": "Backend",
                        "provider": "codex",
                        "status": "success",
                        "summary": "Implemented API",
                        "touched_files": ["src/api.py"],
                        "notes": ["created 1 files"],
                    }
                ],
                subtasks=[
                    SubtaskRun(
                        subtask_id="backend",
                        title="Backend",
                        provider="codex",
                        status="success",
                        handoff_summary="plain summary",
                        handoff_record={
                            "subtask_id": "backend",
                            "title": "Backend",
                            "provider": "codex",
                            "status": "success",
                            "summary": "Implemented API",
                            "touched_files": ["src/api.py"],
                            "notes": ["created 1 files"],
                        },
                    )
                ],
            )

            store.save(session)

            restored = ChatSession(chat_id=7, file_mgr=FileManager(projects_file=str(root / "projects.json")))
            store.load(restored)

            self.assertEqual(restored.last_task_run.handoff_records[0]["subtask_id"], "backend")
            self.assertEqual(restored.last_task_run.subtasks[0].handoff_record["provider"], "codex")


if __name__ == "__main__":
    unittest.main()
