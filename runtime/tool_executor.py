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
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

log = logging.getLogger(__name__)
from typing import Awaitable, Callable
from urllib import error as _url_error, request as _url_request


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
            "name": "fetch_url",
            "description": (
                "Fetch a web page by URL when the user explicitly asks for internet access "
                "or current external information. Returns plain text/html truncated for context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "HTTP or HTTPS URL to fetch."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds. Defaults to 20.", "default": 20},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify",
            "description": (
                "Run this project's own checks - its test suite, and any lint or type "
                "check it defines - and report whether they pass. Use this after making "
                "changes, instead of assuming the work is correct or asking a model to "
                "guess. The commands come from the project's files and were approved by "
                "the user; you cannot choose them. Returns PASS, or FAIL with the output "
                "of whatever failed."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a question and wait for their answer. "
                "Use this when you need clarification, a decision, or information "
                "that you cannot determine from the codebase alone. "
                "The user will be prompted in the Telegram chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user.",
                    },
                },
                "required": ["question"],
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

def _resolve_shell() -> tuple[list[str], str]:
    """Pick the shell to run agent commands in, and say which kind it is.

    On anything POSIX this is just bash. On Windows it matters a great deal
    which bash: `bash` on PATH is normally ``C:\\Windows\\System32\\bash.exe``,
    the WSL launcher. That shell runs inside the WSL virtual machine, which has
    its own filesystem namespace - the Windows C: drive appears there as
    ``/mnt/c``. An agent that writes a file with write_file and then looks for
    it with bash would be working in two different worlds, and the mismatch is
    quiet: commands succeed, they just operate on the wrong filesystem.

    Git Bash and MSYS2 share the Windows filesystem, so they are what we want.
    ``FORGE_SHELL`` overrides the search if someone needs a specific one.
    """
    if os.name != "nt":
        return ["bash", "--norc", "--noprofile"], "bash"

    override = os.getenv("FORGE_SHELL", "").strip()
    if override and Path(override).exists():
        return [override, "--norc", "--noprofile"], "bash"

    candidates = [
        Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
        Path(os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "bin" / "bash.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
        Path(os.getenv("USERPROFILE", "")) / "scoop" / "apps" / "git" / "current" / "bin" / "bash.exe",
        Path(r"C:\msys64\usr\bin\bash.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate), "--norc", "--noprofile"], "bash"

    # Whatever is on PATH, as long as it is not the WSL launcher.
    found = shutil.which("bash")
    if found:
        system_root = Path(os.getenv("SystemRoot", r"C:\Windows")).resolve()
        try:
            Path(found).resolve().relative_to(system_root)
            is_wsl_launcher = True
        except ValueError:
            is_wsl_launcher = False
        if not is_wsl_launcher:
            return [found, "--norc", "--noprofile"], "bash"

    log.warning(
        "No Windows-native bash found (Git Bash or MSYS2); falling back to cmd.exe. "
        "Shell commands written in bash syntax will not work. Install Git for Windows, "
        "or point FORGE_SHELL at a bash that shares the Windows filesystem."
    )
    return ["cmd.exe", "/Q"], "cmd"


class PersistentShell:
    """
    A bash process that lives for the duration of a single agent task.

    Shell state (cwd, env vars, activated venvs) persists across `run()` calls,
    matching CLI providers that run inside a single shell session.

    Usage::

        async with PersistentShell(cwd) as shell:
            output = await shell.run("cd src && python -m pytest", timeout=60)
    """

    def __init__(self, cwd: Path):
        self.cwd = Path(cwd).resolve()
        self._argv, self._kind = _resolve_shell()
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
            *self._argv,
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
            except (Exception, asyncio.TimeoutError):
                pass
            if self._proc and self._proc.returncode is None:
                try:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except (Exception, asyncio.TimeoutError):
                    try:
                        self._proc.kill()
                        await self._proc.wait()
                    except Exception as exc:
                        log.debug("Failed to kill process: %s", exc)

        # asyncio closes the subprocess transport from its __del__, which runs
        # whenever the garbage collector gets to it - by then asyncio.run() has
        # usually closed the loop, and the close fails with "Event loop is
        # closed" followed by an "unclosed transport" ResourceWarning. Nothing
        # breaks, but every run ends in a tracebackul of noise. Close it here,
        # while the loop is still running.
        if self._proc is not None:
            if self._proc.stdin is not None:
                try:
                    self._proc.stdin.close()
                except Exception as exc:
                    log.debug("Failed to close shell stdin: %s", exc)
            transport = getattr(self._proc, "_transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception as exc:
                    log.debug("Failed to close shell transport: %s", exc)

        self._proc = None

    async def run(
        self,
        command: str,
        timeout: int = 30,
        line_callback: "Callable[[str], None] | None" = None,
    ) -> str:
        """Run *command* in the persistent shell and return its output.

        If *line_callback* is provided it is invoked with each non-empty output
        line as it arrives, before the full output is returned.  This gives
        real-time visibility into long-running commands (npm install, pytest, …).
        """
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
                # cmd.exe and Windows tools terminate lines with CRLF; leaving
                # the CR on makes the sentinel comparison below fail and puts a
                # stray carriage return at the end of every captured line.
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if text == sentinel:
                    break
                lines.append(text)
                if line_callback and text.strip():
                    line_callback(text)

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
        interaction_callback: "Callable[[str, str], Awaitable[str | None]] | None" = None,
        delegate: "Callable[[str, str], Awaitable[str]] | None" = None,
        allowed_tools: "tuple[str, ...] | None" = None,
        verify: "Callable[..., Awaitable[str]] | None" = None,
    ):
        self.cwd = Path(cwd).resolve()
        self._notify = notify
        self._shell = shell  # if set, bash calls run inside the persistent process
        self._interaction_callback = interaction_callback
        # Set for a helper agent: only these tools may be called, whatever the
        # model asks for. The advertised tool list is filtered too, so this is a
        # second line rather than the only one - a model that hallucinates a tool
        # it was never offered should be refused, not obeyed.
        self._delegate = delegate
        self._allowed_tools = tuple(allowed_tools) if allowed_tools else None
        self._verify = verify

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def execute(self, tool_name: str, tool_args: dict) -> str:
        """Dispatch a tool call. Always returns a string (never raises)."""
        if self._allowed_tools is not None and tool_name not in self._allowed_tools:
            allowed = ", ".join(self._allowed_tools)
            return f"Error: this agent may not call '{tool_name}'. Available tools: {allowed}."
        try:
            if tool_name == "verify":
                return await self._verify_tool()
            if tool_name == "delegate":
                return await self._delegate_tool(
                    str(tool_args.get("role", "")),
                    str(tool_args.get("task", "")),
                )
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
            if tool_name == "fetch_url":
                return await self._fetch_url(
                    tool_args.get("url", ""),
                    int(tool_args.get("timeout", 20)),
                )
            if tool_name == "search_in_files":
                return await self._search_in_files(
                    tool_args.get("pattern", ""),
                    tool_args.get("path", ""),
                    tool_args.get("file_pattern", ""),
                    bool(tool_args.get("case_sensitive", False)),
                )
            if tool_name == "ask_user":
                return await self._ask_user(tool_args.get("question", ""))
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
        resolved = p.resolve()
        if not self._is_within_workspace(resolved):
            raise PermissionError(f"path escapes working directory: {path}")
        return resolved

    def _is_within_workspace(self, candidate: Path) -> bool:
        try:
            candidate.relative_to(self.cwd)
            return True
        except ValueError:
            return False

    async def _verify_tool(self) -> str:
        if self._verify is None:
            return (
                "Error: verification is not configured in this run. Run the project's "
                "checks with bash instead."
            )
        # The service is handed the executor's own bash so checks run in the same
        # shell, working directory and timeout handling as everything else.
        return await self._verify(
            self.cwd,
            self._bash,
            self._interaction_callback,
            self._notify,
        )

    async def _delegate_tool(self, role: str, task: str) -> str:
        if self._delegate is None:
            return (
                "Error: delegation is not available in this run. Do the work yourself."
            )
        return await self._delegate(role, task)

    async def _bash(self, command: str, timeout: int = 30) -> str:
        timeout = min(max(timeout, 1), BASH_TIMEOUT_MAX)
        short = command[:120] + ("…" if len(command) > 120 else "")
        self._notify(f"🐚 {short}")
        if self._shell is not None:
            def _on_output_line(line: str) -> None:
                stripped = line.strip()
                if stripped:
                    self._notify(f"🐚 {stripped[:200]}")
            return await self._shell.run(command, timeout=timeout, line_callback=_on_output_line)
        # Fallback: standalone subprocess (no state persistence). Resolve the
        # shell the same way PersistentShell does rather than using
        # create_subprocess_shell, which on Windows means cmd.exe - so a command
        # written in bash syntax would fail here while succeeding in the
        # persistent shell, depending only on which path the caller took.
        argv, kind = _resolve_shell()
        run_flag = "/c" if kind == "cmd" else "-c"
        proc = await asyncio.create_subprocess_exec(
            *argv, run_flag, command,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception as exc:
                log.debug("Failed to kill timed-out process: %s", exc)
            return f"[timeout after {timeout}s]"
        finally:
            # Same reason as in PersistentShell.stop: leave this to __del__ and
            # it runs after the loop is gone.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception as exc:
                    log.debug("Failed to close subprocess transport: %s", exc)
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

    def _rel(self, target: Path) -> str:
        """Return path relative to cwd, or just the name if outside cwd."""
        try:
            return str(target.relative_to(self.cwd))
        except ValueError:
            return target.name

    def _write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        self._notify(f"✏️ {target.name}")

        # Snapshot old content before overwriting so we can diff it.
        old_content = ""
        if target.exists():
            try:
                old_content = target.read_text(errors="replace")
            except Exception as exc:
                log.debug("Failed to read old content of %s: %s", target, exc)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

        from runtime.diff_utils import format_diff_notify
        diff_event = format_diff_notify(self._rel(target), old_content, content)
        if diff_event:
            self._notify(diff_event)

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

        from runtime.diff_utils import format_diff_notify
        diff_event = format_diff_notify(self._rel(target), original, updated)
        if diff_event:
            self._notify(diff_event)

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
        matched = []
        for raw_path in _glob.glob(str(base / pattern), recursive=True):
            candidate = Path(raw_path).resolve()
            if not self._is_within_workspace(candidate):
                continue
            matched.append(str(candidate.relative_to(base)))
        matched.sort()
        result = "\n".join(matched)
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return result or "(no matches)"

    async def _fetch_url(self, url: str, timeout: int = 20) -> str:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "Error: fetch_url only supports http:// and https:// URLs"
        timeout = min(max(timeout, 1), 60)
        self._notify(f"🌐 {url[:160]}")

        def _fetch() -> str:
            req = _url_request.Request(
                url,
                headers={"User-Agent": "ForgeLocalLLM/1.0"},
                method="GET",
            )
            with _url_request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(MAX_OUTPUT_CHARS + 1)
                charset = resp.headers.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                if len(raw) > MAX_OUTPUT_CHARS:
                    text += "\n... (truncated)"
                return text

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _fetch)
        except _url_error.URLError as exc:
            return f"Error: network request failed: {exc.reason}"
        except Exception as exc:
            return f"Error: {exc}"

    async def _ask_user(self, question: str) -> str:
        if not question.strip():
            return "Error: question must not be empty"
        if not self._interaction_callback:
            return "[ask_user is not available — no interaction callback configured]"
        self._notify(f"⚙️ Asking user: {question.strip()[:120]}")
        try:
            response = await self._interaction_callback("question", question)
        except Exception as exc:
            return f"[ask_user error: {exc}]"
        if not response or not response.strip():
            return "[no response received from user]"
        return response.strip()

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

        args = ["rg", "-n", "--no-heading", "--color=never"]
        if not case_sensitive:
            args.append("-i")
        if file_pattern:
            args.extend(["--glob", file_pattern])
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
            # ripgrep not available — fall back to Python
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
            except Exception as exc:
                log.debug("Failed to read %s for grep: %s", fpath, exc)
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
