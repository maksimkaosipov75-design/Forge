import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from runtime.verification import (
    Check,
    CheckResult,
    CheckStore,
    VerificationService,
    _split_exit_marker,
    _with_exit_marker,
    detect_checks,
    summarise,
)
from runtime.tool_executor import ToolExecutor


def run(coro):
    return asyncio.run(coro)


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_is_proposed_for_an_empty_directory(self):
        self.assertEqual(detect_checks(self.root), [])

    def test_pytest_is_found_through_pyproject(self):
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'x'\ndependencies = ['pytest']\n", encoding="utf-8"
        )
        commands = [item.command for item in detect_checks(self.root)]
        self.assertIn("python -m pytest -q", commands)

    def test_unittest_layout_is_found_without_any_marker(self):
        """Forge itself is such a project: tests/ and no pytest anywhere."""
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_thing.py").write_text("", encoding="utf-8")
        commands = [item.command for item in detect_checks(self.root)]
        self.assertIn("python -m unittest discover -s tests -q", commands)

    def test_pytest_wins_over_the_layout_guess(self):
        (self.root / "pyproject.toml").write_text("pytest\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_thing.py").write_text("", encoding="utf-8")
        commands = [item.command for item in detect_checks(self.root)]
        self.assertIn("python -m pytest -q", commands)
        self.assertNotIn("python -m unittest discover -s tests -q", commands)

    def test_npm_scripts_become_checks(self):
        (self.root / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}), encoding="utf-8"
        )
        commands = [item.command for item in detect_checks(self.root)]
        self.assertIn("npm run test", commands)
        self.assertIn("npm run lint", commands)

    def test_a_broken_package_json_does_not_raise(self):
        (self.root / "package.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(detect_checks(self.root), [])

    def test_a_project_venv_is_preferred_over_whatever_is_on_path(self):
        """A bare `python` is the wrong interpreter for a project with a venv, and
        the checks then fail on missing dependencies rather than on the code."""
        scripts = self.root / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_text("", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("", encoding="utf-8")
        command = detect_checks(self.root)[0].command
        self.assertTrue(command.startswith(".venv/Scripts/python.exe"), command)

    def test_without_a_venv_the_command_is_plain_python(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("", encoding="utf-8")
        self.assertTrue(detect_checks(self.root)[0].command.startswith("python "))

    def test_only_one_command_per_label(self):
        (self.root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        (self.root / "go.mod").write_text("module x\n", encoding="utf-8")
        labels = [item.label for item in detect_checks(self.root)]
        self.assertEqual(len(labels), len(set(labels)))


class ExitStatusTests(unittest.TestCase):
    """The verdict has to be a fact, not an impression of the output."""

    def test_zero_status_is_a_pass(self):
        passed, output = _split_exit_marker("ran 3 tests\n__FORGE_EXIT_0__")
        self.assertTrue(passed)
        self.assertEqual(output, "ran 3 tests")

    def test_nonzero_status_is_a_failure(self):
        passed, output = _split_exit_marker("1 failed\n__FORGE_EXIT_1__")
        self.assertFalse(passed)
        self.assertEqual(output, "1 failed")

    def test_a_missing_status_is_a_failure_not_a_guess(self):
        """A timeout or dead shell returns no marker. Passing then would tell the
        agent something untrue, which is worse than no check at all."""
        passed, _ = _split_exit_marker("[timeout after 600s]")
        self.assertFalse(passed)

    def test_output_that_merely_mentions_failure_still_passes(self):
        passed, _ = _split_exit_marker("test_handles_failed_login ... ok\n__FORGE_EXIT_0__")
        self.assertTrue(passed)

    def test_the_marker_asks_the_shell_for_the_status(self):
        self.assertIn("$?", _with_exit_marker("pytest"))


class SummaryTests(unittest.TestCase):
    def test_all_passing_is_one_line(self):
        results = [CheckResult(Check("tests", "x", "y"), True, "lots of output")]
        self.assertTrue(summarise(results).startswith("PASS"))

    def test_failures_carry_their_output(self):
        results = [
            CheckResult(Check("tests", "pytest", "pyproject.toml"), False, "E   assert 1 == 2"),
            CheckResult(Check("lint", "ruff", "pyproject.toml"), True, "clean"),
        ]
        text = summarise(results)
        self.assertTrue(text.startswith("FAIL"))
        self.assertIn("assert 1 == 2", text)
        self.assertIn("Passed: lint", text)
        self.assertNotIn("clean", text)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = CheckStore(self.root / "approved.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_unknown_workspace_is_none_not_empty(self):
        """None means 'never asked'; [] means 'asked and declined'."""
        self.assertIsNone(self.store.approved_for(self.root))

    def test_declining_is_remembered(self):
        self.store.approve(self.root, [])
        self.assertEqual(self.store.approved_for(self.root), [])

    def test_approved_commands_survive_a_round_trip(self):
        checks = [Check("tests", "python -m pytest -q", "pyproject.toml")]
        self.store.approve(self.root, checks)
        self.assertEqual(self.store.approved_for(self.root), checks)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("", encoding="utf-8")
        self.store = CheckStore(self.root / "approved.json")
        self.service = VerificationService(self.store)
        self.ran: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    async def _runner(self, command, timeout):
        self.ran.append(command)
        return "ok\n__FORGE_EXIT_0__"

    async def _yes(self, kind, text):
        self.asked = text
        return "yes"

    async def _no(self, kind, text):
        return "no"

    def test_first_run_asks_and_then_runs(self):
        result = run(self.service.verify(self.root, self._runner, ask=self._yes))
        self.assertIn("PASS", result)
        self.assertIn("unittest discover", self.ran[0])
        self.assertIn("Approve for this project?", self.asked)

    def test_approval_is_only_asked_once(self):
        run(self.service.verify(self.root, self._runner, ask=self._yes))

        async def _explode(kind, text):
            raise AssertionError("should not ask again")

        result = run(self.service.verify(self.root, self._runner, ask=_explode))
        self.assertIn("PASS", result)

    def test_declining_runs_nothing_now_or_later(self):
        result = run(self.service.verify(self.root, self._runner, ask=self._no))
        self.assertIn("not approved", result)
        self.assertEqual(self.ran, [])

        result = run(self.service.verify(self.root, self._runner, ask=self._no))
        self.assertIn("No checks are approved", result)
        self.assertEqual(self.ran, [])

    def test_without_a_way_to_ask_nothing_runs(self):
        result = run(self.service.verify(self.root, self._runner, ask=None))
        self.assertIn("approved once", result)
        self.assertEqual(self.ran, [])

    def test_a_project_with_no_checks_says_so(self):
        empty = Path(tempfile.mkdtemp(dir=self.tmp.name))
        result = run(self.service.verify(empty, self._runner, ask=self._yes))
        self.assertIn("No checks could be detected", result)
        self.assertEqual(self.ran, [])

    def test_a_failing_check_is_reported_as_failure(self):
        async def failing(command, timeout):
            return "E   assert 1 == 2\n__FORGE_EXIT_1__"

        run(self.service.verify(self.root, self._runner, ask=self._yes))
        result = run(self.service.verify(self.root, failing))
        self.assertIn("FAIL", result)
        self.assertIn("assert 1 == 2", result)


class VerifyToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_without_a_callback_the_tool_says_so(self):
        executor = ToolExecutor(cwd=self.cwd, notify=lambda _: None)
        result = run(executor.execute("verify", {}))
        self.assertIn("not configured", result)

    def test_the_tool_hands_the_service_its_own_bash(self):
        seen = {}

        async def fake_verify(cwd, run_command, ask, notify):
            seen["cwd"] = cwd
            seen["has_runner"] = callable(run_command)
            return "PASS — all checks passed (tests)."

        executor = ToolExecutor(cwd=self.cwd, notify=lambda _: None, verify=fake_verify)
        result = run(executor.execute("verify", {}))
        self.assertEqual(seen["cwd"], self.cwd)
        self.assertTrue(seen["has_runner"])
        self.assertIn("PASS", result)

    def test_a_helper_cannot_verify(self):
        """Helpers are restricted to their role's tools; verify is not among them."""
        executor = ToolExecutor(
            cwd=self.cwd,
            notify=lambda _: None,
            allowed_tools=("read_file",),
            verify=None,
        )
        result = run(executor.execute("verify", {}))
        self.assertIn("may not call", result)


if __name__ == "__main__":
    unittest.main()
