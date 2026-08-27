import asyncio
import tempfile
import unittest
from pathlib import Path

from runtime.delegation import (
    DELEGATE_TOOL_DEFINITION,
    ROLES,
    DelegationService,
    parse_assignment,
)
from runtime.tool_executor import ToolExecutor


def run(coro):
    return asyncio.run(coro)


class RoleDefinitionTests(unittest.TestCase):
    def test_review_cannot_write_anything(self):
        """The judge-only decision has to be structural, not a rule in a prompt.

        A reviewer that can edit destroys the signal: you can no longer tell
        whether the work was right or the reviewer quietly fixed it.
        """
        for tool in ("write_file", "edit_file", "bash"):
            self.assertNotIn(tool, ROLES["review"].allowed_tools)

    def test_search_cannot_write_anything(self):
        for tool in ("write_file", "edit_file", "bash"):
            self.assertNotIn(tool, ROLES["search"].allowed_tools)

    def test_implement_can_write(self):
        self.assertIn("write_file", ROLES["implement"].allowed_tools)
        self.assertIn("edit_file", ROLES["implement"].allowed_tools)

    def test_no_role_may_delegate(self):
        """Depth is limited by what the roles are allowed to call."""
        for name, role in ROLES.items():
            self.assertNotIn("delegate", role.allowed_tools, f"{name} may delegate")

    def test_tool_enum_matches_the_roles_that_exist(self):
        enum = DELEGATE_TOOL_DEFINITION["function"]["parameters"]["properties"]["role"]["enum"]
        self.assertEqual(sorted(enum), sorted(ROLES))


class AssignmentParsingTests(unittest.TestCase):
    def test_bare_provider_means_its_default_model(self):
        self.assertEqual(parse_assignment("local"), ("local", ""))

    def test_ollama_tag_survives(self):
        """Ollama names carry a colon of their own; only the first one splits."""
        self.assertEqual(
            parse_assignment("local:qwen2.5-coder:7b"),
            ("local", "qwen2.5-coder:7b"),
        )

    def test_openrouter_slug_survives(self):
        self.assertEqual(
            parse_assignment("openrouter:qwen/qwen3-coder:free"),
            ("openrouter", "qwen/qwen3-coder:free"),
        )

    def test_empty_falls_back_to_local(self):
        self.assertEqual(parse_assignment(""), ("local", ""))


class _StubSettings:
    DELEGATION_ENABLED = True
    DELEGATE_SEARCH = "local"
    DELEGATE_REVIEW = "local"
    DELEGATE_IMPLEMENT = "local"


class _StubContainer:
    def __init__(self, provider_paths=None):
        self.provider_paths = provider_paths if provider_paths is not None else {"local": "local"}
        self.built = []

    def build_runtime(self, provider, model_name="", allow_delegation=True):
        self.built.append((provider, model_name, allow_delegation))
        raise AssertionError("not expected to build in these tests")


class DelegationServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = DelegationService(_StubContainer(), _StubSettings())

    def test_unknown_role_is_reported_not_raised(self):
        result = run(self.service.run("architect", "do a thing", Path(".")))
        self.assertIn("unknown role", result)
        self.assertIn("search", result)

    def test_empty_task_is_refused(self):
        result = run(self.service.run("search", "   ", Path(".")))
        self.assertIn("needs a task description", result)

    def test_missing_provider_tells_the_agent_to_do_it_itself(self):
        service = DelegationService(_StubContainer(provider_paths={}), _StubSettings())
        result = run(service.run("search", "find the parser", Path(".")))
        self.assertIn("not available", result)
        self.assertIn("yourself", result)

    def test_assignment_comes_from_settings(self):
        settings = _StubSettings()
        settings.DELEGATE_REVIEW = "openrouter:qwen/qwen3-coder:free"
        service = DelegationService(_StubContainer(), settings)
        self.assertEqual(
            service.assignment_for("review"),
            ("openrouter", "qwen/qwen3-coder:free"),
        )


class ToolAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_restricted_executor_refuses_tools_it_was_not_given(self):
        """Second line of defence: the advertised list is already filtered, but a
        model that invents a tool name should be refused rather than obeyed."""
        executor = ToolExecutor(
            cwd=self.cwd,
            notify=lambda _: None,
            allowed_tools=("read_file", "glob_files"),
        )
        result = run(executor.execute("write_file", {"path": "x.py", "content": "boom"}))
        self.assertIn("may not call", result)
        self.assertFalse((self.cwd / "x.py").exists())

    def test_a_restricted_executor_still_runs_what_it_was_given(self):
        (self.cwd / "note.txt").write_text("hello", encoding="utf-8")
        executor = ToolExecutor(
            cwd=self.cwd,
            notify=lambda _: None,
            allowed_tools=("read_file",),
        )
        result = run(executor.execute("read_file", {"path": "note.txt"}))
        self.assertIn("hello", result)

    def test_delegate_without_a_callback_says_so(self):
        executor = ToolExecutor(cwd=self.cwd, notify=lambda _: None)
        result = run(executor.execute("delegate", {"role": "search", "task": "find it"}))
        self.assertIn("not available", result)

    def test_delegate_reaches_the_callback(self):
        seen = {}

        async def fake_delegate(role, task):
            seen["role"] = role
            seen["task"] = task
            return "cli/app.py:60"

        executor = ToolExecutor(cwd=self.cwd, notify=lambda _: None, delegate=fake_delegate)
        result = run(executor.execute("delegate", {"role": "search", "task": "find textual_main"}))
        self.assertEqual(seen, {"role": "search", "task": "find textual_main"})
        self.assertEqual(result, "cli/app.py:60")


class ToolPayloadTests(unittest.TestCase):
    """What the model is actually offered, which is where the depth limit lives."""

    def _backend(self):
        from runtime.api_backends import OpenRouterExecutionBackend

        return OpenRouterExecutionBackend(
            api_key="test",
            base_url="https://example.invalid/api/v1",
            on_output=lambda _line: None,
            model_name="test/model",
        )

    def test_a_plain_backend_offers_the_full_set_without_delegate(self):
        backend = self._backend()
        names = {item["function"]["name"] for item in backend._tool_payload()}
        self.assertIn("bash", names)
        self.assertIn("read_file", names)
        self.assertNotIn("delegate", names)

    def test_delegate_appears_only_with_a_callback(self):
        backend = self._backend()

        async def _noop(role, task):
            return ""

        backend.delegate_callback = _noop
        names = {item["function"]["name"] for item in backend._tool_payload()}
        self.assertIn("delegate", names)

    def test_a_helper_is_offered_only_its_own_tools(self):
        backend = self._backend()
        backend.allowed_tools = ROLES["review"].allowed_tools
        names = {item["function"]["name"] for item in backend._tool_payload()}
        self.assertEqual(names, set(ROLES["review"].allowed_tools))
        self.assertNotIn("delegate", names)
        self.assertNotIn("write_file", names)


if __name__ == "__main__":
    unittest.main()
