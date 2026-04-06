from __future__ import annotations

import asyncio
import base64 as _base64
import json as _json
import os as _os
import re as _re
import subprocess as _subprocess
import time as _time
from pathlib import Path

_HTML_TAG = _re.compile(r"<[^>]+>")

# ---------------------------------------------------------------------------
# Clipboard helpers (copy / paste without external mandatory dependencies)
# ---------------------------------------------------------------------------

def _clipboard_copy(text: str) -> str:
    """Copy text to clipboard. Returns a status message."""
    # 1. Try native clipboard tools
    for cmd in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["pbcopy"],
    ):
        try:
            _subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=3, check=True)
            return f"Copied ({cmd[0]})."
        except Exception:
            pass
    # 2. OSC 52 terminal escape sequence (kitty, alacritty, wezterm, foot, …)
    try:
        b64 = _base64.b64encode(text.encode("utf-8")).decode()
        _os.write(1, f"\x1b]52;c;{b64}\x07".encode())
        return "Copied via OSC 52 (terminal clipboard)."
    except Exception:
        pass
    # 3. Temp file fallback
    try:
        tmp = Path("/tmp/bridge-clipboard.txt")
        tmp.write_text(text, encoding="utf-8")
        return f"No clipboard tool — saved to {tmp}  (install wl-clipboard for native copy)"
    except Exception:
        pass
    return "Copy failed. Install wl-clipboard: sudo pacman -S wl-clipboard"


def _clipboard_paste() -> str:
    """Read text from clipboard. Returns empty string on failure."""
    for cmd in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["pbpaste"],
    ):
        try:
            result = _subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception:
            pass
    # Fallback: read from temp file written by _clipboard_copy()
    try:
        tmp = Path("/tmp/bridge-clipboard.txt")
        if tmp.exists():
            return tmp.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""

_LANG_MAP = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "rs": "rust", "go": "go", "sh": "bash", "json": "json",
    "yaml": "yaml", "yml": "yaml", "toml": "toml", "md": "markdown",
    "html": "html", "css": "css", "sql": "sql", "nim": "nim",
}


def _strip_html(text: str) -> str:
    return _HTML_TAG.sub("", text)


def _action_from_event(line: str) -> str | None:
    """Map a stream event to a short action label for the status line."""
    if line.startswith("🔧 Использую: ") or line.startswith("🔧 "):
        tool = line.split(": ", 1)[-1].strip() if ": " in line else line[2:].strip()
        return (tool[:38] + "…") if len(tool) > 38 else tool + "…"
    if line.startswith(("✏️ ", "📂 ")):
        parts = line[2:].strip().split()
        return f"Writing {Path(parts[-1]).name}…" if parts else "Writing…"
    if line.startswith("👁️ "):
        parts = line[2:].strip().split()
        return f"Reading {Path(parts[-1]).name}…" if parts else "Reading…"
    if line.startswith("🐚 "):
        cmd = line[2:].strip()
        for pfx in ("Запускаю: ", "Running: "):
            if cmd.startswith(pfx):
                cmd = cmd[len(pfx):]
                break
        return f"$ {cmd[:38]}…" if len(cmd) > 38 else f"$ {cmd}"
    if line.startswith("⚙️ "):
        return "Initializing…"
    if line.startswith("💬 "):
        return "Writing…"
    return None


def _expand_file_mentions(prompt: str, cwd: str) -> str:
    """Replace @path/to/file with the file content inline."""
    import re
    base = Path(cwd)

    def replacer(m: _re.Match) -> str:
        raw_path = m.group(1)
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = base / raw_path
        try:
            if candidate.is_file() and candidate.stat().st_size < 200_000:
                content = candidate.read_text(errors="replace")
                rel = raw_path
                return f"@{rel}\n```\n{content}\n```"
        except Exception:
            pass
        return m.group(0)

    return _re.sub(r"@([\w./\-]+)", replacer, prompt)


_git_status_cache: dict[str, tuple[float, str]] = {}

# ---------------------------------------------------------------------------
# Markdown → Rich markup converter
# ---------------------------------------------------------------------------

def _md_inline_to_rich(text: str) -> str:
    """Convert inline markdown to Rich markup. Assumes [ is already escaped."""
    # Remove markdown links [text](url) → text  (before escaping [ below)
    text = _re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = _re.sub(r'\[([^\]]*)\]\[[^\]]*\]', r'\1', text)

    # Extract inline code before escaping brackets
    code_segs: list[str] = []

    def _save_code(m: _re.Match) -> str:
        code_segs.append(m.group(1))
        return f"\x00CODE{len(code_segs) - 1}\x00"

    text = _re.sub(r'`([^`]+)`', _save_code, text)

    # Escape remaining [ so Rich doesn't mis-parse them
    text = text.replace("[", "\\[")

    # Bold+italic ***text***
    text = _re.sub(r'\*\*\*(.+?)\*\*\*', r'[bold italic]\1[/bold italic]', text)
    # Bold **text** or __text__
    text = _re.sub(r'\*\*(.+?)\*\*', r'[bold]\1[/bold]', text)
    text = _re.sub(r'__(.+?)__', r'[bold]\1[/bold]', text)
    # Italic *text* (not bold) or _text_
    text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'[italic]\1[/italic]', text)
    text = _re.sub(r'(?<!\w)_(?!_)(.+?)_(?!\w)', r'[italic]\1[/italic]', text)

    # Restore inline code with a distinct color
    for i, code in enumerate(code_segs):
        escaped = code.replace("[", "\\[")
        text = text.replace(f"\x00CODE{i}\x00", f"[bold #88dd88]{escaped}[/bold #88dd88]")

    return text


def _md_to_rich(text: str) -> str:
    """Convert a markdown string to Rich markup for display in Textual."""
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []

    for raw_line in text.split("\n"):
        # Code fence detection
        if raw_line.rstrip().startswith("```"):
            if not in_code:
                in_code = True
            else:
                in_code = False
                block = "\n".join(f"  {l}" for l in code_buf)
                out.append("[#888888]" + block.replace("[", "\\[") + "[/#888888]")
                code_buf = []
            continue
        if in_code:
            code_buf.append(raw_line)
            continue

        stripped = raw_line.lstrip()
        indent = len(raw_line) - len(stripped)
        pfx = " " * indent

        # Horizontal rule
        if _re.fullmatch(r'[-*_]{3,}', stripped):
            out.append("[dim]" + "─" * 42 + "[/dim]")
            continue
        # Headings
        if stripped.startswith("#### "):
            out.append(pfx + "[bold dim]" + _md_inline_to_rich(stripped[5:]) + "[/bold dim]")
            continue
        if stripped.startswith("### "):
            out.append(pfx + "[bold]" + _md_inline_to_rich(stripped[4:]) + "[/bold]")
            continue
        if stripped.startswith("## "):
            out.append(pfx + "[bold underline]" + _md_inline_to_rich(stripped[3:]) + "[/bold underline]")
            continue
        if stripped.startswith("# "):
            out.append(pfx + "[bold underline bright_white]" + _md_inline_to_rich(stripped[2:]) + "[/bold underline bright_white]")
            continue
        # Blockquote
        if stripped.startswith("> "):
            out.append(pfx + "[dim italic]▎ " + _md_inline_to_rich(stripped[2:]) + "[/dim italic]")
            continue
        # Unordered list
        if stripped.startswith(("- ", "* ", "+ ")):
            out.append(pfx + "  • " + _md_inline_to_rich(stripped[2:]))
            continue
        # Ordered list
        m = _re.match(r'^(\d+)\.\s+(.*)', stripped)
        if m:
            out.append(pfx + f"  {m.group(1)}. " + _md_inline_to_rich(m.group(2)))
            continue
        # Plain line
        out.append(_md_inline_to_rich(raw_line))

    # Unclosed code block
    if code_buf:
        block = "\n".join(f"  {l}" for l in code_buf)
        out.append("[#888888]" + block.replace("[", "\\[") + "[/#888888]")

    return "\n".join(out)


def _git_status_short(cwd: str) -> str:
    """Return a short git status string, cached for 5 seconds."""
    now = _time.monotonic()
    cached = _git_status_cache.get(cwd)
    if cached and now - cached[0] < 5.0:
        return cached[1]
    try:
        result = _subprocess.run(
            ["git", "status", "--short", "--branch"],
            capture_output=True, text=True, cwd=cwd, timeout=2,
        )
        lines = result.stdout.strip().splitlines()
        if not lines:
            status = ""
        else:
            branch_line = lines[0]  # e.g. "## main...origin/main"
            branch = branch_line.lstrip("#").strip().split("…")[0].split(".")[0].strip()
            changed = len([l for l in lines[1:] if l.strip()])
            status = branch
            if changed:
                status += f" *{changed}"
    except Exception:
        status = ""
    _git_status_cache[cwd] = (now, status)
    return status


def _git_diff(path: Path) -> str | None:
    for args in (
        ["git", "diff", "HEAD", "--", str(path)],
        ["git", "diff", "--", str(path)],
    ):
        try:
            out = _subprocess.run(
                args, capture_output=True, text=True,
                cwd=str(path.parent), timeout=4,
            ).stdout
            if out.strip():
                return out
        except Exception:
            pass
    return None


def _file_diff_text(file_path: str, is_new: bool, max_lines: int = 35, add_color: str = "green") -> list[str]:
    """Return markup lines for a file diff/preview.

    add_color controls the color of added lines (+ lines and new-file content).
    Use the provider's accent color so diffs are visually attributed per agent.
    """
    path = Path(file_path)
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []

    icon = f"[{add_color}]+[/{add_color}]" if is_new else "[yellow]~[/yellow]"
    try:
        n = len(path.read_text(errors="replace").splitlines()) if size < 500_000 else 0
    except Exception:
        n = 0
    size_note = f"  [dim]({n} lines)[/dim]" if n else ""
    lines = [f"\n  {icon} [bold]{path.name}[/bold]{size_note}"]

    if size > 200_000:
        lines.append("  [dim](file too large to preview)[/dim]")
        return lines

    if not is_new:
        diff = _git_diff(path)
        if diff:
            hunk_lines = [
                ln for ln in diff.splitlines()
                if not ln.startswith(("diff ", "index ", "--- ", "+++ "))
            ][:max_lines]
            for ln in hunk_lines:
                if ln.startswith("+"):
                    lines.append(f"  [{add_color}]{ln}[/{add_color}]")
                elif ln.startswith("-"):
                    lines.append(f"  [red]{ln}[/red]")
                elif ln.startswith("@@"):
                    lines.append(f"  [dim]{ln}[/dim]")
                else:
                    lines.append(f"  [dim]{ln}[/dim]")
            if len(hunk_lines) == max_lines:
                lines.append(f"  [dim]... (truncated)[/dim]")
            return lines
        # no git diff — fall through to content

    try:
        content = path.read_text(errors="replace").splitlines()
        shown = content[:max_lines]
        for ln in shown:
            safe = ln.replace("[", "\\[")
            if is_new:
                lines.append(f"  [{add_color}]+ {safe}[/{add_color}]")
            else:
                lines.append(f"  [dim]{safe}[/dim]")
        if len(content) > max_lines:
            lines.append(f"  [dim]... ({len(content) - max_lines} more lines)[/dim]")
    except Exception:
        pass
    return lines


def run_textual_shell(container, chat_id: int = 0):
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Container, Horizontal, VerticalScroll
        from textual.reactive import reactive
        from textual.suggester import Suggester
        from textual.widget import Widget
        from textual.widgets import Input, Static
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError(
            "Textual mode requires the 'textual' package. Install it with './venv/bin/pip install textual'."
        ) from exc

    COMMANDS: dict[str, tuple[str, str]] = {
        "/help": ("Shell", "Show available commands"),
        "/home": ("Shell", "Return to the start page"),
        "/new": ("Shell", "Reset the shell workspace"),
        "/clear": ("Shell", "Clear the stream output"),
        "/provider": ("Providers", "Switch the default provider"),
        "/providers": ("Providers", "List available providers"),
        "/status": ("Status", "Show current shell/session status"),
        "/limits": ("Status", "Show provider health and limits"),
        "/runs": ("History", "List recent runs"),
        "/show": ("History", "Show details for a run by index"),
        "/artifacts": ("History", "List latest artifact files"),
        "/plan": ("Orchestration", "Preview an orchestration plan"),
        "/orchestrate": ("Orchestration", "Run a multi-agent orchestration"),
        "/retry": ("Shell", "Re-run the last prompt"),
        "/expand": ("Shell", "Show full last answer without truncation"),
        "/remote-control": ("Remote", "Start or manage Telegram remote access"),
        "/quit": ("Shell", "Exit the textual shell"),
        "/exit": ("Shell", "Exit the textual shell"),
    }

    class SlashCommandSuggester(Suggester):
        def __init__(self):
            super().__init__(case_sensitive=False)
            self.commands = list(COMMANDS.keys())

        async def get_suggestion(self, value: str) -> str | None:
            # Slash command completion
            if value.startswith("/") and " " not in value.strip():
                lowered = value.casefold()
                for command in self.commands:
                    if command.casefold().startswith(lowered):
                        return command
                return None

            # @file completion — match last @token in value
            m = _re.search(r'@([\w./\-]*)$', value)
            if m:
                partial = m.group(1)
                try:
                    session = container.get_session(chat_id)
                    base = Path(session.file_mgr.get_working_dir())
                    if "/" in partial:
                        parent = base / Path(partial).parent
                        stem = Path(partial).name
                    else:
                        parent = base
                        stem = partial
                    matches = sorted(
                        p for p in parent.iterdir()
                        if p.name.startswith(stem) and not p.name.startswith(".")
                    )
                    if matches:
                        first = matches[0]
                        rel = str(first.relative_to(base))
                        if first.is_dir():
                            rel += "/"
                        return value[: m.start(1)] + rel
                except Exception:
                    pass

            return None

    class TitleWidget(Static):
        pass

    class StreamWidget(Static):
        pass

    class SideWidget(Static):
        pass

    class StatusLineWidget(Static):
        pass

    class MultilinePreview(Static):
        pass

    class BridgeTextualApp(App):
        CSS = """
        Screen {
            background: #111318;
            color: #f3f3f3;
        }

        #titlebar {
            height: 1;
            padding: 0 1;
            background: #1a1d25;
            color: #9aa3b2;
        }

        #workspace {
            margin: 0 1;
            height: 1fr;
        }

        #stream-scroll {
            border: round #6aa7ff;
            height: 1fr;
            width: 1fr;
        }

        #stream {
            padding: 0 1;
            height: auto;
            width: 100%;
        }

        #side {
            border: round #ff9e57;
            padding: 0 1;
            width: 34;
        }

        #statusline {
            height: 1;
            padding: 0 2;
            background: #111318;
            color: #9aa3b2;
            display: none;
        }

        #statusline.active {
            display: block;
        }

        #multiline-preview {
            height: auto;
            max-height: 6;
            padding: 0 2;
            background: #1a1d25;
            color: #9aa3b2;
            display: none;
        }

        #multiline-preview.active {
            display: block;
        }

        #input {
            dock: bottom;
            margin: 0 1 0 1;
        }
        """

        current_provider = reactive("qwen")
        remote_state = reactive("stopped")
        current_mode = reactive("idle")
        current_input = reactive("")

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.timeline_entries: list[str] = ["Shell ready."]
            self.orchestration_steps: list[str] = []
            self._status_state: dict = {"action": "Starting…", "tokens": 0, "start": 0.0, "input_tokens": 0, "output_tokens": 0}
            self._status_timer = None
            self._stream_lines: list[str] = []  # owned copy of stream widget content
            self._last_stream_was_text: bool = False  # for merging consecutive 💬 chunks
            self._active_runtime = None  # for Ctrl+C cancel
            self._input_history: list[str] = []
            self._history_pos: int = -1
            self._history_draft: str = ""  # saves current draft when navigating history
            self._multiline_buffer: list[str] = []  # accumulates lines for multi-line input
            self._ctx_input_tokens: int = 0  # last known context size from API

        def _provider_color(self) -> str:
            mapping = {
                "qwen": "#b07cff",
                "codex": "#6aa7ff",
                "claude": "#ff9e57",
            }
            return mapping.get(self.current_provider, "#6aa7ff")

        def compose(self) -> ComposeResult:
            session = container.get_session(chat_id)
            self.current_provider = session.current_provider
            yield TitleWidget(self._titlebar_text(), id="titlebar")
            with Container(id="workspace"):
                with Horizontal():
                    with VerticalScroll(id="stream-scroll"):
                        yield StreamWidget("Ready.", id="stream")
                    yield SideWidget(self._side_text(), id="side")
            yield StatusLineWidget("", id="statusline")
            yield MultilinePreview("", id="multiline-preview")
            yield Input(
                placeholder="/help · @file · Shift+Enter for multiline · /orchestrate <task>",
                id="input",
                suggester=SlashCommandSuggester(),
            )

        @property
        def _history_path(self) -> Path:
            return Path(".session_data") / "cli_history.json"

        def _load_history(self):
            try:
                data = _json.loads(self._history_path.read_text())
                self._input_history = data.get("history", [])[-100:]
            except Exception:
                pass

        def _save_history(self):
            try:
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                self._history_path.write_text(
                    _json.dumps({"history": self._input_history[-100:]}, ensure_ascii=False)
                )
            except Exception:
                pass

        def on_mount(self):
            self._load_history()
            self._sync_remote_state()
            self._apply_provider_theme()
            self._refresh_all()
            # Auto-refresh sidebar health every 30s
            self.set_interval(30, self._auto_refresh)

        def on_unmount(self):
            self._save_history()

        def _auto_refresh(self):
            """Periodic background refresh: git status, provider health sidebar."""
            # Invalidate git status cache so next titlebar update picks up changes
            cwd = str(container.get_session(chat_id).file_mgr.get_working_dir())
            _git_status_cache.pop(cwd, None)
            self._refresh_all()

        def _titlebar_text(self) -> str:
            session = container.get_session(chat_id)
            cwd = str(session.file_mgr.get_working_dir())
            specialties = {"qwen": "python·data", "codex": "backend·refactor", "claude": "ui·writing"}
            spec = specialties.get(self.current_provider, "general")
            git = _git_status_short(cwd)
            git_part = f"  git:{git}" if git else ""
            ctx_tok = self._ctx_input_tokens
            ctx_part = f"  ctx:~{ctx_tok // 1000}k" if ctx_tok >= 1000 else (f"  ctx:{ctx_tok}" if ctx_tok else "")
            return (
                f">_ {self.current_provider.upper()} [{spec}]  "
                f"{cwd}{git_part}{ctx_part}  "
                f"mode:{self.current_mode}  remote:{self.remote_state}"
            )

        def _provider_specialties(self) -> str:
            mapping = {
                "qwen": "python · scripting · data",
                "codex": "systems · backend · refactor",
                "claude": "ui · ux · writing",
            }
            return mapping.get(self.current_provider, "general purpose")

        def _side_text(self) -> str:
            session = container.get_session(chat_id)
            recent = container.recent_runs(session, limit=5)
            if not recent:
                recent_lines = ["No recent runs yet."]
            else:
                recent_lines = [
                    f"{index}. {run.status_emoji} {run.mode} [{run.provider_summary or 'mixed'}]"
                    for index, run in enumerate(recent, start=1)
                ]
            provider_lines = []
            for provider_name in container.provider_paths:
                runtime = session.runtimes.get(provider_name)
                if runtime is None or runtime.health is None:
                    provider_lines.append(f"{provider_name}: unknown")
                    continue
                health = runtime.health
                state = "up" if health.available else "limited"
                if health.last_failure:
                    state += f" · {health.last_failure.short_label}"
                    if health.last_failure.retry_at:
                        state += f" @ {health.last_failure.retry_at}"
                provider_lines.append(f"{provider_name}: {state}")
            command_hint_lines = self._command_hint_lines()
            return "\n".join(
                [
                    "Remote",
                    f"state: {self.remote_state}",
                    "",
                    "Providers",
                    *provider_lines,
                    "",
                    "Orchestration",
                    *(self.orchestration_steps[-8:] or ["No active plan."]),
                    "",
                    "Timeline",
                    *self.timeline_entries[-8:],
                    "",
                    "Command hints",
                    *command_hint_lines,
                    "",
                    "Recent activity",
                    *recent_lines,
                    "",
                    "Tips",
                    "/new resets the screen",
                    "/plan previews orchestration",
                    "/orchestrate runs multi-agent mode",
                    "/remote-control starts Telegram access",
                ]
            )

        def _command_hint_lines(self) -> list[str]:
            value = self.current_input.strip()
            if not value.startswith("/"):
                return ["Type / to see command suggestions."]
            command_part = value.split(maxsplit=1)[0]
            matches = [
                f"{name} · {meta[0]} · {meta[1]}"
                for name, meta in COMMANDS.items()
                if name.startswith(command_part)
            ]
            return matches[:6] or ["No matching slash commands."]

        def _statusbar_text(self) -> str:
            # kept for compatibility but not rendered
            session = container.get_session(chat_id)
            return " · ".join(
                [
                    f"provider: {self.current_provider}",
                    f"remote: {self.remote_state}",
                    f"mode: {self.current_mode}",
                    f"runs: {len(session.run_history)}",
                ]
            )

        def _sync_remote_state(self):
            from cli.remote_control import RemoteControlManager

            status = RemoteControlManager().load_status()
            self.remote_state = "running" if status.is_running else "stopped"

        def _apply_provider_theme(self):
            color = self._provider_color()
            self.query_one("#stream-scroll", VerticalScroll).styles.border = ("round", color)
            self.query_one("#input", Input).styles.border = ("round", color)

        def _refresh_all(self):
            self.query_one("#titlebar", TitleWidget).update(self._titlebar_text())
            self.query_one("#side", SideWidget).update(self._side_text())

        def _status_renderable(self) -> str:
            elapsed = _time.monotonic() - self._status_state["start"]
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            # Prefer real API token counts; fall back to estimated
            out_tok = self._status_state.get("output_tokens", 0)
            est_tok = self._status_state.get("tokens", 0)
            tokens = out_tok if out_tok else est_tok
            tok_str = f"↑ {tokens / 1000:.1f}k" if tokens >= 1000 else f"↑ {tokens}"
            color = self._provider_color()
            action = self._status_state["action"]
            return f"[{color}]◆[/] {action}  [dim]({time_str} · {tok_str} tokens)[/dim]"

        def _show_status_line(self, provider: str | None = None):
            self._status_state["start"] = _time.monotonic()
            sl = self.query_one("#statusline", StatusLineWidget)
            sl.add_class("active")
            sl.update(self._status_renderable())
            if self._status_timer is not None:
                self._status_timer.stop()
            self._status_timer = self.set_interval(0.5, self._tick_status)

        def _tick_status(self):
            sl = self.query_one("#statusline", StatusLineWidget)
            sl.update(self._status_renderable())

        def _hide_status_line(self):
            if self._status_timer is not None:
                self._status_timer.stop()
                self._status_timer = None
            sl = self.query_one("#statusline", StatusLineWidget)
            sl.remove_class("active")
            sl.update("")

        def _update_status_event(self, line: str):
            action = _action_from_event(line)
            if action:
                self._status_state["action"] = action
            if line.startswith("💬 "):
                self._status_state["tokens"] += max(1, len(line[2:]) // 4)
            elif line.startswith("🔢 "):
                try:
                    parts = line[2:].strip().split(",")
                    inp = int(parts[0])
                    out = int(parts[1]) if len(parts) > 1 else 0
                    self._status_state["input_tokens"] = inp
                    self._status_state["output_tokens"] = out
                    # Update context tracker for titlebar
                    if inp > self._ctx_input_tokens:
                        self._ctx_input_tokens = inp
                        self.query_one("#titlebar", TitleWidget).update(self._titlebar_text())
                except (ValueError, IndexError):
                    pass
            elif line.startswith("❌ ") and "retry" in line.lower():
                # Auto-retry event — surface in status bar
                short = line[2:].strip()[:60]
                self._status_state["action"] = f"⟳ {short}"
            self.query_one("#statusline", StatusLineWidget).update(self._status_renderable())

        def _scroll_to_bottom(self):
            scroll = self.query_one("#stream-scroll", VerticalScroll)
            scroll.scroll_end(animate=False)

        def _set_stream(self, content: str):
            """Replace stream content and sync _stream_lines."""
            self._stream_lines = content.splitlines() if content else []
            self._last_stream_was_text = False
            self.query_one("#stream", StreamWidget).update(content)
            self.call_after_refresh(self._scroll_to_bottom)

        def _append_stream(self, *lines: str):
            self._last_stream_was_text = False
            self._stream_lines.extend(line for line in lines if line is not None)
            self._stream_lines = self._stream_lines[-300:]
            self.query_one("#stream", StreamWidget).update("\n".join(self._stream_lines))
            self.call_after_refresh(self._scroll_to_bottom)

        def _append_stream_event(self, line: str):
            """Format and append a stream event line to the stream widget."""
            color = self._provider_color()
            if line.startswith("💬 "):
                # Escape [ so Rich doesn't mis-parse raw markdown from the model
                chunk = line[2:].strip().replace("[", "\\[")
                if self._last_stream_was_text and self._stream_lines:
                    # Merge with the previous text line — streaming arrives as small chunks
                    self._stream_lines[-1] = self._stream_lines[-1] + chunk
                else:
                    self._stream_lines.append("  " + chunk)
                    self._last_stream_was_text = True
                self._stream_lines = self._stream_lines[-300:]
                self.query_one("#stream", StreamWidget).update("\n".join(self._stream_lines))
                self.call_after_refresh(self._scroll_to_bottom)
                return
            # Any non-text event ends the current text run
            self._last_stream_was_text = False
            if line.startswith("🔧 Использую: ") or line.startswith("🔧 "):
                tool = line.split(": ", 1)[-1].strip() if ": " in line else line[2:].strip()
                formatted = f"  [{color}]✦[/] [dim]{tool.replace('[', chr(92) + '[')}[/dim]"
            elif line.startswith(("✏️ ", "📂 ")):
                formatted = f"  [{color}]✦[/] [dim]{line[2:].strip().replace('[', chr(92) + '[')}[/dim]"
            elif line.startswith("👁️ "):
                formatted = f"  [dim]{line[2:].strip().replace('[', chr(92) + '[')}[/dim]"
            elif line.startswith("🐚 "):
                cmd = line[2:].strip()
                for pfx in ("Запускаю: ", "Running: "):
                    if cmd.startswith(pfx):
                        cmd = cmd[len(pfx):]
                        break
                formatted = f"  [{color}]$[/] [dim]{cmd.replace('[', chr(92) + '[')}[/dim]"
            elif line.startswith("⚙️ "):
                formatted = f"  [dim]{line[2:].strip().replace('[', chr(92) + '[')}[/dim]"
            elif line.startswith(("❌ ", "✅ ")):
                formatted = f"  [dim]{line.replace('[', chr(92) + '[')}[/dim]"
            elif line.startswith(("🧠 ", "🏁 ", "🔢 ")):
                return  # skip thinking, raw completion markers, and token counts
            else:
                return
            self._stream_lines.append(formatted)
            self._stream_lines = self._stream_lines[-300:]
            self.query_one("#stream", StreamWidget).update("\n".join(self._stream_lines))
            self.call_after_refresh(self._scroll_to_bottom)

        def _add_timeline(self, message: str):
            self.timeline_entries.append(message)
            self.timeline_entries = self.timeline_entries[-20:]
            self.query_one("#side", SideWidget).update(self._side_text())

        def _set_orchestration_steps(self, steps: list[str]):
            self.orchestration_steps = steps[-20:]
            self.query_one("#side", SideWidget).update(self._side_text())

        def _mark_orchestration_step(self, step_index: int, state: str):
            if step_index < 0 or step_index >= len(self.orchestration_steps):
                return
            current = self.orchestration_steps[step_index]
            suffix = current.split("] ", 1)[1] if "] " in current else current
            self.orchestration_steps[step_index] = f"[{state}] {suffix}"
            self.query_one("#side", SideWidget).update(self._side_text())

        def action_show_help(self) -> None:
            asyncio.create_task(self._handle_command("/help"))

        def _paste_into_input(self, text: str):
            """Insert text at cursor in the Input widget."""
            if not text:
                return
            try:
                inp = self.query_one("#input", Input)
                inp.focus()
                lines = text.splitlines()
                if len(lines) > 1:
                    # First line into input, remaining into multiline buffer
                    pos = inp.cursor_position
                    val = inp.value
                    inp.value = val[:pos] + lines[0] + val[pos:]
                    inp.cursor_position = pos + len(lines[0])
                    non_empty = [l for l in lines[1:] if l.strip()]
                    if non_empty:
                        self._multiline_buffer.extend(non_empty)
                        self._update_multiline_preview()
                else:
                    pos = inp.cursor_position
                    val = inp.value
                    inp.value = val[:pos] + text + val[pos:]
                    inp.cursor_position = pos + len(text)
            except Exception:
                pass

        def on_paste(self, event) -> None:
            """Handle terminal bracketed-paste and drag-and-drop file paths."""
            text = getattr(event, "text", "")
            if text:
                self._paste_into_input(text)

        def _update_multiline_preview(self):
            preview = self.query_one("#multiline-preview", MultilinePreview)
            if self._multiline_buffer:
                lines = "\n".join(f"  {l}" for l in self._multiline_buffer)
                n = len(self._multiline_buffer)
                preview.update(f"[dim]multiline ({n} lines — Enter to send, Esc to cancel):[/dim]\n{lines}")
                preview.add_class("active")
            else:
                preview.update("")
                preview.remove_class("active")

        async def on_key(self, event) -> None:
            if event.key == "?":
                await self._handle_command("/help")
                return
            if event.key == "escape":
                if self._multiline_buffer:
                    self._multiline_buffer = []
                    self._update_multiline_preview()
                    event.prevent_default()
                    return
            if event.key == "ctrl+v":
                # Explicit clipboard paste fallback (for terminals without bracketed paste)
                text = _clipboard_paste()
                if text:
                    self._paste_into_input(text)
                else:
                    self._append_stream(
                        "  [dim]Ctrl+V: clipboard empty or no tool available.[/dim]",
                        "  [dim]Install wl-clipboard:  sudo pacman -S wl-clipboard[/dim]",
                        "  [dim]Or use Ctrl+Shift+V (terminal bracketed-paste) to paste directly.[/dim]",
                    )
                event.prevent_default()
                return
            if event.key == "ctrl+y":
                # Copy last answer to clipboard
                session = container.get_session(chat_id)
                answer = ""
                if session.last_task_result:
                    answer = session.last_task_result.answer_text.strip()
                if answer:
                    msg = _clipboard_copy(answer)
                    self._add_timeline(msg[:80])
                    self._append_stream(f"  [dim]{msg}[/dim]")
                else:
                    self._add_timeline("Nothing to copy yet.")
                event.prevent_default()
                return
            if event.key == "shift+enter":
                input_widget = self.query_one("#input", Input)
                val = input_widget.value
                if val.strip():
                    self._multiline_buffer.append(val)
                    input_widget.value = ""
                    self.current_input = ""
                    self._update_multiline_preview()
                event.prevent_default()
                return
            if event.key == "ctrl+c":
                if self._active_runtime is not None:
                    runtime_to_stop = self._active_runtime
                    self._active_runtime = None
                    asyncio.create_task(runtime_to_stop.manager.stop())
                    self._append_stream("  [yellow]↩ Cancelled[/yellow]")
                    self._hide_status_line()
                    self.current_mode = "idle"
                return
            input_widget = self.query_one("#input", Input)
            if event.key == "up":
                if not self._input_history:
                    return
                if self._history_pos == -1:
                    self._history_draft = input_widget.value
                    self._history_pos = len(self._input_history) - 1
                elif self._history_pos > 0:
                    self._history_pos -= 1
                input_widget.value = self._input_history[self._history_pos]
                input_widget.cursor_position = len(input_widget.value)
                event.prevent_default()
                return
            if event.key == "down":
                if self._history_pos == -1:
                    return
                if self._history_pos < len(self._input_history) - 1:
                    self._history_pos += 1
                    input_widget.value = self._input_history[self._history_pos]
                else:
                    self._history_pos = -1
                    input_widget.value = self._history_draft
                input_widget.cursor_position = len(input_widget.value)
                event.prevent_default()
                return

        async def on_input_changed(self, event: Input.Changed) -> None:
            self.current_input = event.value
            self.query_one("#side", SideWidget).update(self._side_text())

        async def on_input_submitted(self, event: Input.Submitted):
            value = event.value.strip()
            event.input.value = ""
            self.current_input = ""
            self._history_pos = -1
            self._history_draft = ""
            self.query_one("#side", SideWidget).update(self._side_text())

            # Merge multiline buffer if active
            if self._multiline_buffer:
                parts = self._multiline_buffer + ([value] if value else [])
                value = "\n".join(parts)
                self._multiline_buffer = []
                self._update_multiline_preview()

            if not value:
                return

            # Push to history (deduplicate consecutive identical entries)
            # Store the first line as history key for multi-line prompts
            history_key = value.split("\n")[0][:120]
            if not self._input_history or self._input_history[-1] != history_key:
                self._input_history.append(history_key)
                if len(self._input_history) > 100:
                    self._input_history = self._input_history[-100:]
            self._save_history()

            suggestion = getattr(event.input, "_suggestion", "")
            if value.startswith("/") and " " not in value and suggestion and suggestion != value:
                value = suggestion

            if value.startswith("/"):
                await self._handle_command(value)
                return

            await self._run_prompt(value)

        async def _handle_command(self, raw: str):
            stream = self.query_one("#stream", StreamWidget)
            session = container.get_session(chat_id)
            parts = raw.split(maxsplit=1)
            command = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if command in {"/quit", "/exit"}:
                self.exit()
                return
            if command in {"/home", "/new"}:
                self.current_mode = "idle"
                self._set_orchestration_steps([])
                self._add_timeline("Workspace reset.")
                self._set_stream("Ready.")
                self._refresh_all()
                return
            if command == "/clear":
                self._set_stream("")
                self._add_timeline("Stream cleared.")
                return
            if command == "/retry":
                last = session.last_task_result
                if last and last.prompt:
                    await self._run_prompt(last.prompt)
                else:
                    self._set_stream("[dim]No previous prompt to retry.[/dim]")
                return
            if command == "/expand":
                last = session.last_task_result
                if last and last.answer_text.strip():
                    color = self._provider_color()
                    self._set_stream(
                        f"[{color}]◆ {last.provider}[/]  [dim]full answer[/dim]\n\n"
                        + _md_to_rich(last.answer_text.strip())
                    )
                else:
                    self._set_stream("[dim]No answer to expand.[/dim]")
                return
            if command == "/help":
                self._add_timeline("Opened help.")
                self._set_stream(
                    "\n".join(
                        [
                            "/help",
                            "/home · /new · /clear",
                            "/provider <qwen|codex|claude>",
                            "/status · /limits",
                            "/runs · /show <n>",
                            "/retry",
                            "/expand",
                            "/remote-control [status|stop|logs]",
                            "/plan <task>",
                            "/orchestrate <task>",
                            "",
                            "Keys:",
                            "  Ctrl+Y      copy last answer to clipboard",
                            "  Ctrl+V      paste from clipboard",
                            "  Shift+Enter start multi-line input",
                            "  Esc         cancel multi-line buffer",
                            "  ↑ / ↓       input history",
                            "  Ctrl+C      cancel active task",
                            "",
                            "Drag a file into the terminal to @mention it.",
                        ]
                    )
                )
                return
            if command == "/provider":
                if not arg:
                    self._set_stream(f"provider: {session.current_provider}")
                    return
                from providers import is_supported_provider, normalize_provider_name

                provider = normalize_provider_name(arg)
                if not is_supported_provider(provider):
                    self._set_stream(f"Unsupported provider: {arg}")
                    return
                session.current_provider = provider
                container.save_session(session)
                self.current_provider = provider
                self._apply_provider_theme()
                self._add_timeline(f"Provider set to {provider}.")
                self._refresh_all()
                self._set_stream(f"Default provider set to {provider}.")
                return
            if command == "/providers":
                self._set_stream(
                    "\n".join(
                        f"{name} · {path}"
                        for name, path in container.provider_paths.items()
                    )
                )
                return
            if command == "/status":
                self._add_timeline("Viewed status.")
                self._set_stream(
                    "\n".join(
                        [
                            f"provider: {session.current_provider}",
                            f"working_dir: {session.file_mgr.get_working_dir()}",
                            f"runs: {len(session.run_history)}",
                            f"remote: {self.remote_state}",
                        ]
                    )
                )
                return
            if command == "/limits":
                self._add_timeline("Viewed limits.")
                provider_lines: list[str] = []
                for provider_name in container.provider_paths:
                    runtime = session.runtimes.get(provider_name)
                    if runtime is None or runtime.health is None:
                        provider_lines.append(f"{provider_name}: availability unknown · context unknown")
                        continue
                    health = runtime.health
                    line = f"{provider_name}: {'available' if health.available else 'limited'} · context {health.context_status}"
                    if health.last_failure:
                        line += f" · {health.last_failure.short_label}"
                        if health.last_failure.retry_at:
                            line += f" · retry {health.last_failure.retry_at}"
                    provider_lines.append(line)
                self._set_stream("\n".join(provider_lines))
                return
            if command == "/runs":
                self._add_timeline("Viewed runs.")
                runs = container.recent_runs(session, limit=10)
                if not runs:
                    self._set_stream("No runs yet.")
                    return
                self._set_stream(
                    "\n".join(
                        f"{index}. {run.status_emoji} {run.mode} [{run.provider_summary or 'mixed'}]"
                        for index, run in enumerate(runs, start=1)
                    )
                )
                return
            if command == "/show":
                if not arg:
                    self._set_stream("Usage: /show <index>")
                    return
                try:
                    index = int(arg)
                except ValueError:
                    self._set_stream("Run index must be a number.")
                    return
                run = container.run_by_index(session, index)
                if run is None:
                    self._set_stream(f"Run {index} not found.")
                    return
                details = [
                    f"run_id: {run.run_id}",
                    f"status: {run.status}",
                    f"mode: {run.mode}",
                    f"complexity: {run.complexity}",
                ]
                if run.strategy:
                    details.append(f"strategy: {run.strategy}")
                if run.provider_summary:
                    details.append(f"providers: {run.provider_summary}")
                if run.artifact_file:
                    details.append(f"artifact: {run.artifact_file}")
                if run.subtasks:
                    details.append("")
                    details.append("subtasks:")
                    details.extend(
                        f"- {item.subtask_id}: {item.title} [{item.provider}] ({item.status})"
                        for item in run.subtasks
                    )
                self._set_stream("\n".join(details))
                return
            if command == "/artifacts":
                artifacts = container.latest_artifact_files(session)
                if not artifacts:
                    self._set_stream("No artifacts found.")
                    return
                self._set_stream("\n".join(str(item) for item in artifacts))
                return
            if command == "/plan":
                if not arg:
                    self._set_stream("Usage: /plan <task>")
                    return
                plan = container.build_planner(session).build_plan(arg)
                session.last_plan = plan
                container.save_session(session)
                self._add_timeline(f"Planned: {arg[:48]}")
                self._set_stream(
                    "\n".join(
                        [
                            f"complexity: {plan.complexity}",
                            f"strategy: {plan.strategy}",
                            "",
                            *[
                                f"{index}. {item.title} [{item.suggested_provider}]"
                                for index, item in enumerate(plan.subtasks, start=1)
                            ],
                        ]
                    )
                )
                return
            if command == "/orchestrate":
                if not arg:
                    self._set_stream("Usage: /orchestrate <task>")
                    return
                await self._run_orchestration(arg)
                return
            if command == "/remote-control":
                from cli.remote_control import RemoteControlManager

                manager = RemoteControlManager()
                action = arg or "start"
                if action == "start":
                    try:
                        status = manager.start()
                    except RuntimeError as exc:
                        self._set_stream(str(exc))
                        return
                    self.remote_state = "running" if status.is_running else "stopped"
                    self._add_timeline("Remote control started.")
                    self._refresh_all()
                    self._set_stream(f"Remote control started. log: {status.log_path}")
                    return
                if action == "status":
                    status = manager.load_status()
                    self.remote_state = "running" if status.is_running else "stopped"
                    self._add_timeline("Checked remote-control status.")
                    self._refresh_all()
                    self._set_stream(
                        "\n".join(
                            [
                                f"running: {'yes' if status.is_running else 'no'}",
                                f"pid: {status.pid or '-'}",
                                f"log_path: {status.log_path or '-'}",
                            ]
                        )
                    )
                    return
                if action == "stop":
                    status = manager.stop()
                    self.remote_state = "running" if status.is_running else "stopped"
                    self._add_timeline("Remote control stopped.")
                    self._refresh_all()
                    self._set_stream("Remote control stopped.")
                    return
                if action == "logs":
                    logs = manager.tail_logs()
                    self._set_stream(logs or "No remote-control logs yet.")
                    return
                self._set_stream("Usage: /remote-control [status|stop|logs]")
                return

            self._set_stream(f"Unknown command: {raw}")

        async def _run_prompt(self, prompt: str):
            from providers import normalize_provider_name

            session = container.get_session(chat_id)
            provider_name = normalize_provider_name(session.current_provider)
            runtime = await container.ensure_runtime_started(session, provider_name)
            stream = self.query_one("#stream", StreamWidget)
            self.current_mode = "single"
            self._active_runtime = runtime
            self._set_orchestration_steps([])
            self._add_timeline(f"Started: {prompt[:48]}")
            self._refresh_all()

            cwd = str(session.file_mgr.get_working_dir())
            expanded_prompt = _expand_file_mentions(prompt, cwd)
            color = self._provider_color()
            self._set_stream(
                f"[{color}]◆[/] [{color}]{provider_name}[/]  [dim]{cwd}[/dim]\n"
                f"  [dim]{prompt[:120]}[/dim]\n"
            )

            self._status_state = {"action": "Starting…", "tokens": 0, "start": 0.0, "input_tokens": 0, "output_tokens": 0}
            self._last_stream_was_text = False
            self._show_status_line(provider_name)

            def stream_event_callback(line: str):
                self._update_status_event(line)
                self._append_stream_event(line)

            result = await container.execution_service.execute_provider_task(
                session=session,
                runtime=runtime,
                provider_name=provider_name,
                prompt=expanded_prompt,
                stream_event_callback=stream_event_callback,
            )
            self._hide_status_line()
            self._active_runtime = None
            container.remember_task_result(session, result)
            self.current_mode = "idle"
            self._add_timeline(f"Done exit_code={result.exit_code}.")
            self._refresh_all()

            # File diffs — use provider's accent color for added lines
            diff_lines: list[str] = []
            for f in result.new_files:
                diff_lines.extend(_file_diff_text(f, is_new=True, add_color=color))
            for f in result.changed_files:
                diff_lines.extend(_file_diff_text(f, is_new=False, add_color=color))

            # Final result
            status_icon = f"[{color}]✓[/]" if result.exit_code == 0 else "[red]✗[/red]"
            duration = getattr(result, "duration_text", "")
            final_parts = [
                "",
                *diff_lines,
            ]
            if result.answer_text.strip():
                final_parts += ["", _md_to_rich(result.answer_text.strip()[:3000])]
            elif result.exit_code != 0 and result.error_text:
                final_parts += ["", f"[red]{result.error_text[:500]}[/red]"]
            final_parts += [
                "",
                f"  {status_icon} [{color}]{provider_name}[/]  [dim]{duration}[/dim]",
            ]
            self._append_stream(*final_parts)

        async def _run_orchestration(self, prompt: str):
            session = container.get_session(chat_id)
            stream = self.query_one("#stream", StreamWidget)
            self.current_mode = "orchestrated"
            self._add_timeline("Started orchestration.")
            self._refresh_all()
            cwd_for_expand = str(session.file_mgr.get_working_dir())
            expanded_prompt = _expand_file_mentions(prompt, cwd_for_expand)
            plan = container.build_planner(session).build_plan(expanded_prompt)
            session.last_plan = plan
            container.save_session(session)
            self._set_orchestration_steps(
                [
                    *[
                        f"[pending] {index}. {item.title} [{item.suggested_provider}]"
                        for index, item in enumerate(plan.subtasks, start=1)
                    ],
                    "[pending] synthesis",
                    "[pending] review",
                ]
            )
            cwd = str(session.file_mgr.get_working_dir())
            color = self._provider_color()
            self._set_stream(
                "\n".join([
                    f"[{color}]◆[/] orchestrate  [dim]{cwd}[/dim]",
                    f"  [dim]{prompt[:120]}[/dim]",
                    "",
                    f"  strategy: {plan.strategy}",
                    *[
                        f"  [dim]{i}. {item.title} [{item.suggested_provider}][/dim]"
                        for i, item in enumerate(plan.subtasks, start=1)
                    ],
                ])
            )
            current_step = {"index": -1}
            step_start = [_time.monotonic()]

            self._status_state = {"action": "Planning…", "tokens": 0, "start": 0.0, "input_tokens": 0, "output_tokens": 0}
            self._last_stream_was_text = False
            self._show_status_line()

            async def status_callback(text: str):
                if not text:
                    return
                clean = _strip_html(text).strip()
                lowered = clean.lower()
                if "шаг " in lowered and "агент:" in lowered:
                    next_index = min(current_step["index"] + 1, max(0, len(plan.subtasks) - 1))
                    if current_step["index"] >= 0:
                        self._mark_orchestration_step(current_step["index"], "done")
                    current_step["index"] = next_index
                    self._mark_orchestration_step(next_index, "running")
                    subtask = plan.subtasks[next_index]
                    step_start[0] = _time.monotonic()
                    self._status_state.update({"action": f"Step {next_index+1}/{len(plan.subtasks)}…", "tokens": 0, "start": step_start[0]})
                    step_color = self._provider_color()
                    self._append_stream(
                        "",
                        f"[dim]{'─' * 38}[/dim]",
                        f"[{step_color}]▶[/] [bold]Step {next_index+1}/{len(plan.subtasks)}[/bold]  {subtask.title}  [dim][{subtask.suggested_provider}][/dim]",
                        f"  [dim]{cwd}[/dim]",
                    )
                elif "собирает итог" in lowered:
                    if 0 <= current_step["index"] < len(plan.subtasks):
                        self._mark_orchestration_step(current_step["index"], "done")
                    self._mark_orchestration_step(len(plan.subtasks), "running")
                    self._status_state.update({"action": "Synthesizing…", "tokens": 0, "start": _time.monotonic()})
                    self._append_stream("", f"[dim]{'─' * 38}[/dim]", f"[{color}]▶[/] [bold]Synthesis[/bold]")
                elif "выполняет review" in lowered:
                    self._mark_orchestration_step(len(plan.subtasks), "done")
                    self._mark_orchestration_step(len(plan.subtasks) + 1, "running")
                    self._status_state.update({"action": "Reviewing…", "tokens": 0, "start": _time.monotonic()})
                    self._append_stream("", f"[dim]{'─' * 38}[/dim]", f"[{color}]▶[/] [bold]Review[/bold]")
                self._add_timeline(clean[:72])

            def stream_event_callback(line: str):
                self._update_status_event(line)
                self._append_stream_event(line)

            task_run, aggregate_result = await container.orchestrator_service.run_orchestrated_task(
                session=session,
                plan=plan,
                status_callback=status_callback,
                stream_event_callback=stream_event_callback,
            )
            self._hide_status_line()
            self._active_runtime = None
            self.current_mode = "idle"
            if 0 <= current_step["index"] < len(plan.subtasks):
                self._mark_orchestration_step(current_step["index"], "done")
            self._mark_orchestration_step(len(plan.subtasks), "done")
            self._mark_orchestration_step(
                len(plan.subtasks) + 1,
                "done" if task_run.review_answer else "skipped",
            )
            self._add_timeline(f"Done status={task_run.status}.")
            self._refresh_all()

            # Per-subtask file diffs
            result_lines: list[str] = [
                "",
                f"[dim]{'═' * 38}[/dim]",
                f"status: {task_run.status}  [dim]{cwd}[/dim]",
            ]
            for subtask in task_run.subtasks:
                st_icon = "[green]✓[/green]" if subtask.status == "success" else "[red]✗[/red]"
                subtask_color = {
                    "qwen": "#b07cff", "codex": "#6aa7ff", "claude": "#ff9e57",
                }.get(subtask.provider, "#6aa7ff")
                result_lines.append(f"\n  {st_icon} [{subtask_color}]{subtask.provider}[/]  [dim]{subtask.title}[/dim]")
                for f in subtask.new_files[:6]:
                    result_lines.extend(_file_diff_text(f, is_new=True, add_color=subtask_color))
                for f in subtask.changed_files[:6]:
                    result_lines.extend(_file_diff_text(f, is_new=False, add_color=subtask_color))
                if subtask.answer_text.strip():
                    excerpt = subtask.answer_text.strip()[:300].replace("\n", " ").replace("[", "\\[")
                    result_lines.append(f"  [dim]> {excerpt}[/dim]")

            if task_run.review_answer:
                result_lines += ["", "[bold]Review:[/bold]", _md_to_rich(task_run.review_answer.strip()[:600])]
            elif aggregate_result.answer_text.strip():
                result_lines += ["", _md_to_rich(aggregate_result.answer_text.strip()[:2000])]

            self._append_stream(*result_lines)

    BridgeTextualApp().run()
