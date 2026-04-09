import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from file_manager import FileManager
from orchestrator import OrchestrationPlan, PlannedSubtask
from runtime.orchestrator_service import OrchestratorService
from task_models import ChatSession, SubtaskRun, TaskRun


class OrchestratorServiceTests(unittest.TestCase):
    def test_pick_healthy_provider_skips_api_when_cli_fallback_required(self):
        class DummyContainer:
            provider_paths = {"qwen": "qwen", "codex": "codex", "openrouter": "https://openrouter.ai/api/v1"}

        session = type("S", (), {"runtimes": {}})()
        service = OrchestratorService(DummyContainer(), execution_service=None)
        service._is_provider_available = lambda _session, name: name != "qwen"

        picked = service._pick_healthy_provider(session, "qwen", allow_api=False)

        self.assertEqual(picked, "codex")

    def test_find_alt_provider_skips_api_when_cli_fallback_required(self):
        class DummyContainer:
            provider_paths = {"qwen": "qwen", "openrouter": "https://openrouter.ai/api/v1", "claude": "claude"}

        session = type("S", (), {"runtimes": {}})()
        service = OrchestratorService(DummyContainer(), execution_service=None)
        service._is_provider_available = lambda _session, name: name != "qwen"

        picked = service._find_alt_provider(session, "qwen", allow_api=False)

        self.assertEqual(picked, "claude")

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

    def test_validate_plan_rejects_future_dependency_for_ordered_v1(self):
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
                ),
                PlannedSubtask(
                    subtask_id="backend",
                    title="Backend",
                    description="Build backend",
                    task_kind="backend_core",
                    suggested_provider="codex",
                    reason="backend fit",
                ),
            ],
        )

        error = OrchestratorService.validate_plan(plan)

        self.assertIsNotNone(error)
        self.assertIn("ordered-v1", error)

    def test_validate_plan_rejects_duplicate_subtask_ids(self):
        plan = OrchestrationPlan(
            prompt="build app",
            complexity="simple",
            strategy="split",
            subtasks=[
                PlannedSubtask(
                    subtask_id="dup",
                    title="A",
                    description="first",
                    task_kind="general",
                    suggested_provider="qwen",
                    reason="fit",
                ),
                PlannedSubtask(
                    subtask_id="dup",
                    title="B",
                    description="second",
                    task_kind="general",
                    suggested_provider="codex",
                    reason="fit",
                ),
            ],
        )

        error = OrchestratorService.validate_plan(plan)

        self.assertEqual(error, "Duplicate subtask_id: dup")

    def test_run_orchestrated_task_fails_fast_on_invalid_plan(self):
        class DummyStore:
            def write_run_artifact(self, session, task_run):
                return str(Path(session.file_mgr.get_working_dir()) / "artifact.md")

            def clear_checkpoint(self, chat_id):
                return None

        class DummyMetrics:
            def __init__(self):
                self.statuses = []

            def record_orchestrated_run(self, status):
                self.statuses.append(status)

        class DummyContainer:
            def __init__(self, workdir: Path):
                self.session_store = DummyStore()
                self.metrics = DummyMetrics()
                self.saved = 0
                self.provider_paths = {"qwen": "qwen", "codex": "codex", "claude": "claude"}
                self.workdir = workdir

            def save_session(self, session):
                self.saved += 1

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
                ),
                PlannedSubtask(
                    subtask_id="backend",
                    title="Backend",
                    description="Build backend",
                    task_kind="backend_core",
                    suggested_provider="codex",
                    reason="backend fit",
                ),
            ],
        )

        with TemporaryDirectory() as tmpdir:
            session = ChatSession(chat_id=1, file_mgr=FileManager(projects_file=str(Path(tmpdir) / "projects.json")))
            session.file_mgr.working_dir = Path(tmpdir)
            container = DummyContainer(Path(tmpdir))
            service = OrchestratorService(container, execution_service=None)

            task_run, aggregate_result = asyncio.run(service.run_orchestrated_task(session, plan))

        self.assertEqual(task_run.status, "failed")
        self.assertEqual(aggregate_result.exit_code, 1)
        self.assertIn("ordered-v1", aggregate_result.error_text)
        self.assertEqual(container.metrics.statuses, ["failed"])


if __name__ == "__main__":
    unittest.main()
