"""
Tool executor for the OpenRouter agentic loop.

Provides TOOL_DEFINITIONS (sent to the LLM) and ToolExecutor (runs the calls).
PersistentShell keeps a bash process alive for the duration of a task so that
`cd`, environment variables, and activated virtualenvs persist across calls —
matching the behaviour of CLI providers (qwen/codex/claude) that run natively
inside a shell session.
"""
from __future__ import annotations

import asyncio
import glob as _glob
import uuid
from pathlib import Path
from typing import Callable


MAX_OUTPUT_CHARS = 8000
BASH_TIMEOUT_MAX = 120


TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command. Shell state persists across calls: "
                "`cd`, exported variables, and activated virtualenvs carry over. "
                "Use for running tests, installing packages, git operations, "
                "compiling code, and any other shell work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Timeout in seconds (max {BASH_TIMEOUT_MAX}). Defaults to 30.",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the file.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file."},
                    "content": {"type": "string", "description": "Content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact string in a file with a new string. "
                "Fails if old_str is not found or is not unique. "
                "Prefer over write_file for targeted edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {
                        "type": "string",
                        "description": "Exact text to find (must be unique in file).",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory to list. Defaults to working directory.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py'.",
                    },
                    "base_dir": {
                        "type": "string",
                        "description": "Base directory. Defaults to working directory.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": (
                "Search for a text pattern across files (like grep -rn). "
                "Returns matching lines with file path and line number. "
                "Use to find function definitions, usages, or any text across the codebase."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search in. Defaults to working directory.",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Limit search to files matching this glob, e.g. '*.py'. Optional.",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Case-sensitive search. Defaults to false.",
                        "default": False,
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Persistent shell
# ---------------------------------------------------------------------------

class PersistentShell:
    """
    A bash process that lives for the duration of a single agent task.

    Shell state (cwd, env vars, activated venvs) persists across `run()` calls,
    matching CLI providers that run inside a single shell session.

    Usage::

        async with PersistentShell(cwd) as shell:
            output = await shell.run("cd src && python -m pytest", timeout=60)
    """

    _BASH = ["bash", "--norc", "--noprofile"]

    def __init__(self, cwd: Path):
        self.cwd = Path(cwd).resolve()
        self._proc: asyncio.subprocess.Process | None = None
        self._sentinel = f"__FORGE_{uuid.uuid4().hex}__"
        self._lock = asyncio.Lock()  # serialise concurrent bash calls

    async def __aenter__(self) -> "PersistentShell":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._BASH,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.cwd),
        )

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.stdin.write(b"exit 0\n")
                await self._proc.stdin.drain()
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except Exception:
                pass
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass
        self._proc = None

    async def run(self, command: str, timeout: int = 30) -> str:
        """Run *command* in the persistent shell and return its output."""
        async with self._lock:
            if not self._proc or self._proc.returncode is not None:
                return "[shell not running]"

            sentinel = self._sentinel
            # Run command directly (not in a subshell) so that cd, export, and
            # venv activation persist across calls. The sentinel prints on its
            # own line regardless of the command's exit code.
            script = f"{command}\necho {sentinel}\n"
            try:
                self._proc.stdin.write(script.encode("utf-8", errors="replace"))
                await self._proc.stdin.drain()
            except Exception as exc:
                return f"[write error: {exc}]"

            lines: list[str] = []
            deadline = asyncio.get_event_loop().time() + timeout

            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    lines.append(f"[timeout after {timeout}s]")
                    break
                try:
                    raw = await asyncio.wait_for(
                        self._proc.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    lines.append(f"[timeout after {timeout}s]")
                    break
                if not raw:
                    lines.append("[shell process terminated]")
                    break
                text = raw.decode("utf-8", errors="replace").rstrip("\n")
                if text == sentinel:
                    break
                lines.append(text)

            output = "\n".join(lines)
            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
            return output


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

class ToolExecutor:
    """Executes tool calls issued by the LLM during an agentic loop.

    All notify calls emit raw emoji-prefixed strings (e.g. ``🐚 ls``),
    which pass through the parser's emoji filter and the Telegram stream
    renderer without any additional decoding.
    """

    def __init__(
        self,
        cwd: Path,
        notify: Callable[[str], None],
        shell: PersistentShell | None = None,
    ):
        self.cwd = Path(cwd).resolve()
        self._notify = notify
        self._shell = shell  # if set, bash calls run inside the persistent process

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def execute(self, tool_name: str, tool_args: dict) -> str:
        """Dispatch a tool call. Always returns a string (never raises)."""
        try:
            if tool_name == "bash":
                return await self._bash(
                    tool_args.get("command", ""),
                    int(tool_args.get("timeout", 30)),
                )
            if tool_name == "read_file":
                return self._read_file(tool_args.get("path", ""))
            if tool_name == "write_file":
                return self._write_file(
                    tool_args.get("path", ""),
                    tool_args.get("content", ""),
                )
            if tool_name == "edit_file":
                return self._edit_file(
                    tool_args.get("path", ""),
                    tool_args.get("old_str", ""),
                    tool_args.get("new_str", ""),
                )
            if tool_name == "list_directory":
                return self._list_directory(tool_args.get("path", ""))
            if tool_name == "glob_files":
                return self._glob_files(
                    tool_args.get("pattern", ""),
                    tool_args.get("base_dir", ""),
                )
            if tool_name == "search_in_files":
                return await self._search_in_files(
                    tool_args.get("pattern", ""),
                    tool_args.get("path", ""),
                    tool_args.get("file_pattern", ""),
                    bool(tool_args.get("case_sensitive", False)),
                )
            return f"Error: unknown tool '{tool_name}'"
        except Exception as exc:
            return f"Error: {exc}"

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self.cwd / p
        return p.resolve()

    async def _bash(self, command: str, timeout: int = 30) -> str:
        timeout = min(max(timeout, 1), BASH_TIMEOUT_MAX)
        short = command[:120] + ("…" if len(command) > 120 else "")
        self._notify(f"🐚 {short}")
        if self._shell is not None:
            return await self._shell.run(command, timeout=timeout)
        # Fallback: standalone subprocess (no state persistence)
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return f"[timeout after {timeout}s]"
        output = stdout.decode("utf-8", errors="replace")
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return output

    def _read_file(self, path: str) -> str:
        target = self._resolve(path)
        self._notify(f"👁️ {target.name}")
        content = target.read_text(errors="replace")
        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return content

    def _write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        self._notify(f"✏️ {target.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Written {len(content)} bytes to {path}"

    def _edit_file(self, path: str, old_str: str, new_str: str) -> str:
        target = self._resolve(path)
        self._notify(f"✏️ {target.name}")
        original = target.read_text(errors="replace")
        count = original.count(old_str)
        if count == 0:
            return f"Error: old_str not found in {path}"
        if count > 1:
            return f"Error: old_str is not unique in {path} ({count} occurrences)"
        updated = original.replace(old_str, new_str, 1)
        target.write_text(updated)
        return f"Edited {path}"

    def _list_directory(self, path: str = "") -> str:
        target = self._resolve(path) if path else self.cwd
        self._notify(f"📂 {target.name}/")
        entries = sorted(
            (e.name + "/" if e.is_dir() else e.name)
            for e in target.iterdir()
        )
        result = "\n".join(entries)
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return result

    def _glob_files(self, pattern: str, base_dir: str = "") -> str:
        base = self._resolve(base_dir) if base_dir else self.cwd
        self._notify(f"🔍 {pattern}")
        matched = sorted(
            str(Path(p).relative_to(base))
            for p in _glob.glob(str(base / pattern), recursive=True)
        )
        result = "\n".join(matched)
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return result or "(no matches)"

    async def _search_in_files(
        self,
        pattern: str,
        path: str = "",
        file_pattern: str = "",
        case_sensitive: bool = False,
    ) -> str:
        target = self._resolve(path) if path else self.cwd
        short_pat = pattern[:60] + ("…" if len(pattern) > 60 else "")
        self._notify(f"🔍 {short_pat!r}")

        args = ["grep", "-rn", "--color=never"]
        if not case_sensitive:
            args.append("-i")
        if file_pattern:
            args.extend(["--include", file_pattern])
        # Exclude common noise directories
        for skip in ("__pycache__", ".git", "node_modules", ".venv", "venv", "dist", "build"):
            args.extend(["--exclude-dir", skip])
        args.extend(["--", pattern, str(target)])

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace")
            if not output.strip():
                return "(no matches)"
            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
            return output
        except asyncio.TimeoutError:
            return "[timeout after 30s]"
        except FileNotFoundError:
            # grep not available — fall back to Python
            return self._search_python(pattern, target, file_pattern, case_sensitive)

    def _search_python(
        self,
        pattern: str,
        target: Path,
        file_pattern: str,
        case_sensitive: bool,
    ) -> str:
        """Pure-Python grep fallback when system grep is unavailable."""
        import re

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            rx = re.compile(pattern, flags)
        except re.error as exc:
            # Treat as literal string if regex is invalid
            rx = re.compile(re.escape(pattern), flags)

        _skip = {"__pycache__", ".git", "node_modules", ".venv", "venv", "dist", "build"}
        lines: list[str] = []

        files = [target] if target.is_file() else []
        if target.is_dir():
            for p in target.rglob("*"):
                if any(part in _skip for part in p.parts):
                    continue
                if not p.is_file():
                    continue
                if file_pattern and not p.match(file_pattern):
                    continue
                files.append(p)

        for fpath in files:
            try:
                text = fpath.read_text(errors="replace")
            except Exception:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    try:
                        rel = fpath.relative_to(self.cwd)
                    except ValueError:
                        rel = fpath
                    lines.append(f"{rel}:{lineno}:{line}")
                    if len(lines) >= 200:
                        break
            if len(lines) >= 200:
                lines.append("... (truncated at 200 matches)")
                break

        result = "\n".join(lines)
        if not result:
            return "(no matches)"
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return result
