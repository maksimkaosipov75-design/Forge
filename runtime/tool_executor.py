"""
Tool executor for the OpenRouter agentic loop.

Provides TOOL_DEFINITIONS (sent to the LLM) and ToolExecutor (runs the calls).
"""
from __future__ import annotations

import asyncio
import glob as _glob
import os
from pathlib import Path
from typing import Callable

from core.event_protocol import encode_forge_event


MAX_OUTPUT_CHARS = 8000
BASH_TIMEOUT_MAX = 60


TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command in the working directory. "
                "Use for running tests, installing packages, listing files, "
                "git operations, compiling code, etc."
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
                "Fails if old_str is not found or is not unique."
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
                        "description": "Base directory to search from. Defaults to working directory.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]


class ToolExecutor:
    """Executes tool calls issued by the LLM during an agentic loop."""

    def __init__(
        self,
        cwd: Path,
        notify: Callable[[str], None],
    ):
        self.cwd = Path(cwd).resolve()
        self._notify = notify

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
        self._notify(encode_forge_event("tool", text=f"🐚 {command[:120]}"))
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
        self._notify(encode_forge_event("tool", text=f"👁️ {target.name}"))
        content = target.read_text(errors="replace")
        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return content

    def _write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        self._notify(encode_forge_event("tool", text=f"✏️ {target.name}"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Written {len(content)} bytes to {path}"

    def _edit_file(self, path: str, old_str: str, new_str: str) -> str:
        target = self._resolve(path)
        self._notify(encode_forge_event("tool", text=f"✏️ {target.name}"))
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
        self._notify(encode_forge_event("tool", text=f"📂 {target.name}/"))
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
        self._notify(encode_forge_event("tool", text=f"🔍 {pattern}"))
        matched = sorted(
            str(Path(p).relative_to(base))
            for p in _glob.glob(str(base / pattern), recursive=True)
        )
        result = "\n".join(matched)
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return result or "(no matches)"
