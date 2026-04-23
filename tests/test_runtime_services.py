import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.file_manager import FileManager
from core.orchestrator import OrchestrationPlan, PlannedSubtask
from runtime.orchestrator_service import OrchestratorService
from core.task_models import ChatSession, SubtaskRun, TaskRun


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

    def test_next_ready_group_uses_dependencies(self):
        plan = OrchestrationPlan(
            prompt="build app",
            complexity="complex",
            strategy="split",
            subtasks=[
                PlannedSubtask(
                    subtask_id="backend",
                    title="Backend",
                    description="Build backend",
                    task_kind="backend_core",
                    suggested_provider="codex",
                    reason="backend fit",
                    parallel_group=0,
                ),
                PlannedSubtask(
                    subtask_id="docs",
                    title="Docs",
                    description="Write docs",
                    task_kind="docs",
                    suggested_provider="qwen",
                    reason="docs fit",
                    parallel_group=0,
                ),
                PlannedSubtask(
                    subtask_id="ui",
                    title="UI",
                    description="Build UI",
                    task_kind="ui_surface",
                    suggested_provider="claude",
                    reason="ui fit",
                    depends_on=["backend"],
                    parallel_group=1,
                ),
            ],
        )
        run = TaskRun(run_id="run-1", prompt="build app")

        first_group = OrchestratorService._next_ready_group(plan, run)

        self.assertEqual([item[1].subtask_id for item in first_group], ["backend", "docs"])

        run.subtasks.append(SubtaskRun(subtask_id="backend", title="Backend", provider="codex", status="success"))
        run.subtasks.append(SubtaskRun(subtask_id="docs", title="Docs", provider="qwen", status="success"))

        second_group = OrchestratorService._next_ready_group(plan, run)

        self.assertEqual([item[1].subtask_id for item in second_group], ["ui"])

    def test_blocked_subtasks_report_missing_dependencies(self):
        plan = OrchestrationPlan(
            prompt="build app",
            complexity="complex",
            strategy="split",
            subtasks=[
                PlannedSubtask(
                    subtask_id="backend",
                    title="Backend",
                    description="Build backend",
                    task_kind="backend_core",
                    suggested_provider="codex",
                    reason="backend fit",
                ),
                PlannedSubtask(
                    subtask_id="ui",
                    title="UI",
                    description="Build UI",
                    task_kind="ui_surface",
                    suggested_provider="claude",
                    reason="ui fit",
                    depends_on=["backend"],
                ),
            ],
        )
        run = TaskRun(
            run_id="run-1",
            prompt="build app",
            subtasks=[SubtaskRun(subtask_id="backend", title="Backend", provider="codex", status="failed")],
        )

        blocked = OrchestratorService._blocked_subtasks(plan, run)

        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0][1].subtask_id, "ui")
        self.assertEqual(blocked[0][2], ["backend"])

    def test_build_handoff_record_is_structured(self):
        result = type(
            "Result",
            (),
            {
                "provider": "qwen",
                "model_name": "test-model",
                "transport": "cli",
                "exit_code": 0,
                "answer_text": "Implemented parser and CLI",
                "error_text": "",
                "new_files": ["src/parser.py"],
                "changed_files": ["src/cli.py"],
            },
        )()

        record = OrchestratorService.build_handoff_record("parser", "Parser", result)

        self.assertEqual(record["subtask_id"], "parser")
        self.assertEqual(record["provider"], "qwen")
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["touched_files"], ["src/parser.py", "src/cli.py"])
        self.assertTrue(record["notes"])

    def test_expand_subtask_into_children_inserts_nested_steps(self):
        class DummyContainer:
            provider_paths = {"qwen": "qwen", "codex": "codex", "claude": "claude"}

        service = OrchestratorService(DummyContainer(), execution_service=None)
        plan = OrchestrationPlan(
            prompt="Build a complex backend service",
            complexity="complex",
            strategy="split",
            subtasks=[
                PlannedSubtask(
                    subtask_id="backend",
                    title="Backend",
                    description="Implement the backend service and contracts",
                    task_kind="backend_core",
                    suggested_provider="codex",
                    reason="backend fit",
                    parallel_group=1,
                ),
                PlannedSubtask(
                    subtask_id="review",
                    title="Review",
                    description="Review the service",
                    task_kind="review",
                    suggested_provider="claude",
                    reason="review fit",
                    depends_on=["backend"],
                    parallel_group=2,
                ),
            ],
        )

        expanded = service._expand_subtask_into_children(plan, plan.subtasks[0])

        self.assertTrue(expanded)
        self.assertEqual(plan.subtasks[0].parent_subtask_id, "backend")
        self.assertEqual(plan.subtasks[1].parent_subtask_id, "backend")
        self.assertEqual(plan.subtasks[0].depth, 1)
        self.assertEqual(plan.subtasks[1].depth, 1)
        self.assertEqual(plan.subtasks[2].subtask_id, "backend")
        self.assertEqual(plan.subtasks[2].depends_on, ["backend--impl", "backend--verify"])


if __name__ == "__main__":
    unittest.main()
