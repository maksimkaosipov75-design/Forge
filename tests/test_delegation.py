import asyncio
import tempfile
import unittest
from pathlib import Path

from runtime.delegation import (
    DELEGATE_TOOL_DEFINITION,
    ROLES,
    DelegationService,
    describe_helpers,
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
    LOCAL_LLM_BASE_URL = "http://127.0.0.1:11434/v1"


class _Model:
    def __init__(self, name):
        self.name = name


class _StubContainer:
    def __init__(self, provider_paths=None, ready=True, ready_reason="", installed=()):
        self.provider_paths = provider_paths if provider_paths is not None else {"local": "local"}
        self.built = []
        self._ready = ready
        self._ready_reason = ready_reason
        self._installed = set(installed)
        self.catalogue = ["qwen2.5-coder:7b", "devstral:latest", "llama3.1:8b"]

    def provider_is_ready(self, provider):
        return (self._ready, self._ready_reason)

    def list_available_models(self, provider, refresh=False, tools_only=False):
        return [_Model(name) for name in self.catalogue]

    def local_model_is_installed(self, name, refresh=False):
        return name in self._installed

    def build_runtime(self, provider, model_name="", allow_delegation=True):
        self.built.append((provider, model_name, allow_delegation))
        raise AssertionError("not expected to build in these tests")


class RoleResolutionTests(unittest.TestCase):
    def test_an_unreachable_provider_is_reported_not_worked_around(self):
        container = _StubContainer(ready=False, ready_reason="OpenRouter API key is not configured.")
        service = DelegationService(container, _StubSettings())
        resolution = service.resolve_role("search")
        self.assertFalse(resolution.ok)
        self.assertIn("API key", resolution.reason)

    def test_a_configured_local_model_that_is_installed_is_used(self):
        container = _StubContainer(installed={"qwen2.5-coder:7b"})
        settings = _StubSettings()
        settings.DELEGATE_SEARCH = "local:qwen2.5-coder:7b"
        resolution = DelegationService(container, settings).resolve_role("search")
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.model, "qwen2.5-coder:7b")
        self.assertEqual(resolution.reason, "")

    def test_a_missing_local_model_falls_back_within_the_same_provider(self):
        """Substituting one free local model for another spends nothing."""
        container = _StubContainer(installed={"devstral:latest"})
        settings = _StubSettings()
        settings.DELEGATE_SEARCH = "local:qwen2.5-coder:7b"
        resolution = DelegationService(container, settings).resolve_role("search")
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.model, "devstral:latest")
        self.assertIn("not installed", resolution.reason)

    def test_nothing_installed_is_reported_with_the_address_that_was_tried(self):
        container = _StubContainer(installed=set())
        resolution = DelegationService(container, _StubSettings()).resolve_role("search")
        self.assertFalse(resolution.ok)
        self.assertIn("11434", resolution.reason)

    def test_the_catalogue_is_not_trusted_about_what_is_installed(self):
        """The curated catalogue lists models the server may not have. Trusting it
        once made every role report ready while nothing was listening at all."""
        container = _StubContainer(installed=set())
        self.assertTrue(container.list_available_models("local"))  # catalogue is non-empty
        resolution = DelegationService(container, _StubSettings()).resolve_role("search")
        self.assertFalse(resolution.ok)

    def test_a_hosted_provider_is_never_substituted_for_a_local_one(self):
        """Sliding to a paid provider because Ollama is down would spend money to
        save a sentence. The agent is told to do the work itself instead."""
        container = _StubContainer(installed=set())
        result = run(DelegationService(container, _StubSettings()).run("search", "find it", Path(".")))
        self.assertIn("unavailable", result)
        self.assertIn("yourself", result)
        self.assertEqual(container.built, [])


class CliProviderAsHelperTests(unittest.TestCase):
    """A CLI provider is another agent with its own tools. Forge can give it a
    prompt but cannot take its tools away, so roles defined by a restriction
    cannot be honoured there."""

    def _service(self, assignment):
        settings = _StubSettings()
        settings.DELEGATE_SEARCH = assignment
        settings.DELEGATE_REVIEW = assignment
        settings.DELEGATE_IMPLEMENT = assignment
        return DelegationService(_StubContainer(installed={"devstral:latest"}), settings)

    def test_review_refuses_a_cli_agent(self):
        resolution = self._service("codex").resolve_role("review")
        self.assertFalse(resolution.ok)
        self.assertIn("read-only", resolution.reason)
        self.assertIn("codex", resolution.reason)

    def test_search_refuses_a_cli_agent(self):
        resolution = self._service("claude").resolve_role("search")
        self.assertFalse(resolution.ok)
        self.assertIn("read-only", resolution.reason)

    def test_implement_accepts_a_cli_agent(self):
        """implement is defined by what it is asked to do, not by what it may not
        touch, so a CLI agent can carry it."""
        resolution = self._service("codex").resolve_role("implement")
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.provider, "codex")

    def test_read_only_roles_still_work_on_api_providers(self):
        resolution = self._service("local").resolve_role("review")
        self.assertTrue(resolution.ok)

    def test_every_read_only_role_declares_it(self):
        """The flag and the tool list must not drift apart: a role with no write
        tools is making a promise it needs the flag to keep."""
        writes = {"write_file", "edit_file", "bash"}
        for name, role in ROLES.items():
            read_only = not (writes & set(role.allowed_tools))
            self.assertEqual(
                read_only, role.requires_restriction,
                f"{name}: read_only={read_only} but requires_restriction={role.requires_restriction}",
            )


class DescribeHelpersTests(unittest.TestCase):
    """/helpers has to answer honestly on a machine where nothing is available,
    which is the only state the author can currently observe."""

    class _Container(_StubContainer):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.delegation = DelegationService(self, _StubSettings())

            class _Verification:
                @staticmethod
                def describe(workspace):
                    return ["  tests  python -m pytest -q"]

            self.verification = _Verification()

    def test_unavailable_roles_say_why(self):
        text = describe_helpers(self._Container(installed=set()), Path("."))
        self.assertIn("unavailable", text)
        self.assertIn("11434", text)

    def test_a_substitution_is_shown_not_hidden(self):
        container = self._Container(installed={"devstral:latest"})
        text = describe_helpers(container, Path("."))
        self.assertIn("devstral:latest", text)

    def test_checks_are_included(self):
        text = describe_helpers(self._Container(installed=set()), Path("."))
        self.assertIn("Checks", text)
        self.assertIn("pytest", text)

    def test_every_role_appears(self):
        text = describe_helpers(self._Container(installed=set()), Path("."))
        for name in ROLES:
            self.assertIn(name, text)


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

    def test_an_unusable_helper_tells_the_agent_to_do_it_itself(self):
        service = DelegationService(_StubContainer(installed=set()), _StubSettings())
        result = run(service.run("search", "find the parser", Path(".")))
        self.assertIn("unavailable", result)
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
