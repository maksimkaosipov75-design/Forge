import asyncio
import tempfile
import unittest
from pathlib import Path

from runtime.tool_executor import ToolExecutor, MAX_OUTPUT_CHARS


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
    # bash
    # ------------------------------------------------------------------

    def test_bash_returns_output(self):
        result = run(self.executor.execute("bash", {"command": "echo hello"}))
        self.assertEqual(result.strip(), "hello")

    def test_bash_emits_tool_event(self):
        run(self.executor.execute("bash", {"command": "echo hi"}))
        self.assertTrue(any("🐚" in e for e in self.events))

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

    def test_read_file_emits_event(self):
        (self.cwd / "f.txt").write_text("a")
        run(self.executor.execute("read_file", {"path": "f.txt"}))
        self.assertTrue(any("👁️" in e for e in self.events))

    # ------------------------------------------------------------------
    # write_file
    # ------------------------------------------------------------------

    def test_write_file_creates_file(self):
        run(self.executor.execute("write_file", {"path": "new.txt", "content": "data"}))
        self.assertEqual((self.cwd / "new.txt").read_text(), "data")

    def test_write_file_creates_parent_dirs(self):
        run(self.executor.execute("write_file", {"path": "sub/dir/f.txt", "content": "x"}))
        self.assertTrue((self.cwd / "sub" / "dir" / "f.txt").exists())

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

    # ------------------------------------------------------------------
    # unknown tool
    # ------------------------------------------------------------------

    def test_unknown_tool_returns_error(self):
        result = run(self.executor.execute("nonexistent", {}))
        self.assertIn("Error", result)
        self.assertIn("unknown tool", result)


if __name__ == "__main__":
    unittest.main()
