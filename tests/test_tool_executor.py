import asyncio
import tempfile
import unittest
from pathlib import Path

from runtime.tool_executor import MAX_OUTPUT_CHARS, PersistentShell, ToolExecutor


def run(coro):
    return asyncio.run(coro)


class ToolExecutorTests(unittest.TestCase):
    def setUp(self):
        self.events: list[str] = []
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.executor = ToolExecutor(cwd=self.cwd, notify=self.events.append)

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # bash (no persistent shell — fallback mode)
    # ------------------------------------------------------------------

    def test_bash_returns_output(self):
        result = run(self.executor.execute("bash", {"command": "echo hello"}))
        self.assertEqual(result.strip(), "hello")

    def test_bash_emits_raw_emoji_event(self):
        """Notification must be a raw '🐚 …' string, NOT a forge-encoded event."""
        run(self.executor.execute("bash", {"command": "echo hi"}))
        self.assertTrue(any(e.startswith("🐚") for e in self.events))
        # Must NOT be a forge-encoded wrapper
        self.assertFalse(any(e.startswith("FORGE_EVENT") for e in self.events))

    def test_bash_timeout_returns_timeout_message(self):
        result = run(self.executor.execute("bash", {"command": "sleep 10", "timeout": 1}))
        self.assertIn("timeout", result)

    def test_bash_captures_stderr(self):
        result = run(self.executor.execute("bash", {"command": "echo err >&2"}))
        self.assertIn("err", result)

    # ------------------------------------------------------------------
    # read_file
    # ------------------------------------------------------------------

    def test_read_file_returns_content(self):
        p = self.cwd / "hello.txt"
        p.write_text("world")
        result = run(self.executor.execute("read_file", {"path": "hello.txt"}))
        self.assertEqual(result, "world")

    def test_read_file_truncates_large_content(self):
        p = self.cwd / "big.txt"
        p.write_text("x" * (MAX_OUTPUT_CHARS + 100))
        result = run(self.executor.execute("read_file", {"path": "big.txt"}))
        self.assertIn("truncated", result)
        self.assertLessEqual(len(result), MAX_OUTPUT_CHARS + 50)

    def test_read_file_emits_raw_emoji_event(self):
        (self.cwd / "f.txt").write_text("a")
        run(self.executor.execute("read_file", {"path": "f.txt"}))
        self.assertTrue(any(e.startswith("👁️") for e in self.events))
        self.assertFalse(any(e.startswith("FORGE_EVENT") for e in self.events))

    # ------------------------------------------------------------------
    # write_file
    # ------------------------------------------------------------------

    def test_write_file_creates_file(self):
        run(self.executor.execute("write_file", {"path": "new.txt", "content": "data"}))
        self.assertEqual((self.cwd / "new.txt").read_text(), "data")

    def test_write_file_creates_parent_dirs(self):
        run(self.executor.execute("write_file", {"path": "sub/dir/f.txt", "content": "x"}))
        self.assertTrue((self.cwd / "sub" / "dir" / "f.txt").exists())

    def test_write_file_emits_raw_emoji_event(self):
        run(self.executor.execute("write_file", {"path": "w.txt", "content": "x"}))
        self.assertTrue(any(e.startswith("✏️") for e in self.events))

    # ------------------------------------------------------------------
    # edit_file
    # ------------------------------------------------------------------

    def test_edit_file_replaces_string(self):
        p = self.cwd / "code.py"
        p.write_text("foo = 1\nbar = 2\n")
        run(self.executor.execute("edit_file", {"path": "code.py", "old_str": "foo = 1", "new_str": "foo = 99"}))
        self.assertIn("foo = 99", p.read_text())

    def test_edit_file_errors_if_not_found(self):
        p = self.cwd / "code.py"
        p.write_text("foo = 1\n")
        result = run(self.executor.execute("edit_file", {"path": "code.py", "old_str": "zzz", "new_str": "aaa"}))
        self.assertIn("Error", result)
        self.assertIn("not found", result)

    def test_edit_file_errors_if_not_unique(self):
        p = self.cwd / "code.py"
        p.write_text("x = 1\nx = 1\n")
        result = run(self.executor.execute("edit_file", {"path": "code.py", "old_str": "x = 1", "new_str": "x = 2"}))
        self.assertIn("Error", result)
        self.assertIn("unique", result)

    # ------------------------------------------------------------------
    # list_directory
    # ------------------------------------------------------------------

    def test_list_directory_lists_files(self):
        (self.cwd / "a.txt").write_text("")
        (self.cwd / "b.py").write_text("")
        result = run(self.executor.execute("list_directory", {}))
        self.assertIn("a.txt", result)
        self.assertIn("b.py", result)

    # ------------------------------------------------------------------
    # glob_files
    # ------------------------------------------------------------------

    def test_glob_files_finds_pattern(self):
        (self.cwd / "a.py").write_text("")
        (self.cwd / "b.py").write_text("")
        (self.cwd / "c.txt").write_text("")
        result = run(self.executor.execute("glob_files", {"pattern": "*.py"}))
        self.assertIn("a.py", result)
        self.assertIn("b.py", result)
        self.assertNotIn("c.txt", result)

    def test_glob_files_no_match(self):
        result = run(self.executor.execute("glob_files", {"pattern": "*.rs"}))
        self.assertIn("no matches", result)

    def test_glob_files_emits_raw_emoji_event(self):
        run(self.executor.execute("glob_files", {"pattern": "*.py"}))
        self.assertTrue(any(e.startswith("🔍") for e in self.events))

    # ------------------------------------------------------------------
    # search_in_files
    # ------------------------------------------------------------------

    def test_search_finds_pattern(self):
        (self.cwd / "a.py").write_text("def hello():\n    pass\n")
        (self.cwd / "b.py").write_text("x = 1\n")
        result = run(self.executor.execute("search_in_files", {"pattern": "def hello"}))
        self.assertIn("hello", result)
        self.assertIn("a.py", result)

    def test_search_no_match(self):
        (self.cwd / "a.py").write_text("x = 1\n")
        result = run(self.executor.execute("search_in_files", {"pattern": "NONEXISTENT_XYZ"}))
        self.assertIn("no matches", result)

    def test_search_case_insensitive_by_default(self):
        (self.cwd / "a.py").write_text("HELLO = 1\n")
        result = run(self.executor.execute("search_in_files", {"pattern": "hello"}))
        self.assertIn("HELLO", result)

    def test_search_case_sensitive(self):
        (self.cwd / "a.py").write_text("HELLO = 1\nhello = 2\n")
        result = run(self.executor.execute("search_in_files", {
            "pattern": "hello", "case_sensitive": True
        }))
        # Should find lowercase "hello" but not "HELLO"
        self.assertIn("hello = 2", result)

    def test_search_with_file_pattern(self):
        (self.cwd / "a.py").write_text("needle\n")
        (self.cwd / "b.txt").write_text("needle\n")
        result = run(self.executor.execute("search_in_files", {
            "pattern": "needle", "file_pattern": "*.py"
        }))
        self.assertIn("a.py", result)
        self.assertNotIn("b.txt", result)

    def test_search_emits_raw_emoji_event(self):
        run(self.executor.execute("search_in_files", {"pattern": "anything"}))
        self.assertTrue(any(e.startswith("🔍") for e in self.events))

    # ------------------------------------------------------------------
    # unknown tool
    # ------------------------------------------------------------------

    def test_unknown_tool_returns_error(self):
        result = run(self.executor.execute("nonexistent", {}))
        self.assertIn("Error", result)
        self.assertIn("unknown tool", result)


class PersistentShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_basic_command(self):
        async def _run():
            async with PersistentShell(self.cwd) as sh:
                return await sh.run("echo persistent")
        result = run(_run())
        self.assertIn("persistent", result)

    def test_state_persists_across_calls(self):
        """cd in first call should persist for the second call."""
        async def _run():
            sub = self.cwd / "sub"
            sub.mkdir()
            async with PersistentShell(self.cwd) as sh:
                await sh.run(f"cd {sub}")
                return await sh.run("pwd")
        result = run(_run())
        self.assertIn("sub", result)

    def test_env_variable_persists(self):
        async def _run():
            async with PersistentShell(self.cwd) as sh:
                await sh.run("export MY_VAR=hello123")
                return await sh.run("echo $MY_VAR")
        result = run(_run())
        self.assertIn("hello123", result)

    def test_timeout_returns_message(self):
        async def _run():
            async with PersistentShell(self.cwd) as sh:
                return await sh.run("sleep 10", timeout=1)
        result = run(_run())
        self.assertIn("timeout", result)

    def test_failed_command_still_returns_output(self):
        """The sentinel must appear even after a failing command."""
        async def _run():
            async with PersistentShell(self.cwd) as sh:
                return await sh.run("echo before_fail && false && echo after_fail")
        result = run(_run())
        self.assertIn("before_fail", result)

    def test_executor_uses_persistent_shell(self):
        """ToolExecutor with PersistentShell should preserve cd across bash calls."""
        events: list[str] = []

        async def _run():
            sub = self.cwd / "subdir"
            sub.mkdir()
            async with PersistentShell(self.cwd) as sh:
                ex = ToolExecutor(cwd=self.cwd, notify=events.append, shell=sh)
                await ex.execute("bash", {"command": f"cd {sub}"})
                return await ex.execute("bash", {"command": "pwd"})

        result = run(_run())
        self.assertIn("subdir", result)


if __name__ == "__main__":
    unittest.main()
