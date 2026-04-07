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


_SPIN_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_PASSWORD_RE = _re.compile(
    r'\[sudo\]|password\s*(?:for\s+\w+\s*)?:|passphrase\s*:|enter\s+password|authentication\s+required',
    _re.IGNORECASE,
)
_DOT_FRAMES = ("·  ", "·· ", "···", "·· ")


def _action_from_event(line: str) -> str | None:
    """Map a stream event to a short action label for the status line."""
    if line.startswith("🔧 Использую: ") or line.startswith("🔧 "):
        tool = line.split(": ", 1)[-1].strip() if ": " in line else line[2:].strip()
        return (tool[:38] + "…") if len(tool) > 38 else tool + "…"
    if line.startswith(("✏️ ", "📂 ")):
        raw = line.split(None, 1)[-1].strip() if " " in line else ""
        fname = Path(raw.split()[-1]).name if raw.split() else ""
        return f"Writing {fname}…" if fname else "Writing…"
    if line.startswith("👁️ "):
        raw = line.split(None, 1)[-1].strip() if " " in line else ""
        fname = Path(raw.split()[-1]).name if raw.split() else ""
        return f"Reading {fname}…" if fname else "Reading…"
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
# Streaming line renderer — same logic as _md_to_rich but per-line with state
# ---------------------------------------------------------------------------

def _render_stream_line(raw_line: str, in_code: bool) -> tuple[str, bool]:
    """Render one line of markdown text for inline streaming display.

    Returns (rich_markup_string, new_in_code_state).
    Mirrors _md_to_rich's per-line logic so the live render matches the final render.
    """
    stripped_r = raw_line.rstrip()
    stripped = raw_line.lstrip()

    # Code fence toggle
    if stripped_r.lstrip().startswith("```"):
        lang = stripped_r.lstrip()[3:].strip() if not in_code else ""
        label = f"[dim]{lang}[/dim]" if lang else ""
        return (f"  [dim]```[/dim]{label}", True) if not in_code else ("  [dim]```[/dim]", False)

    if in_code:
        safe = raw_line.replace("[", "\\[")
        return f"  [#888888]{safe}[/#888888]", True

    if not stripped.strip():
        return "", False

    indent = len(raw_line) - len(stripped)
    pfx = " " * indent

    # Horizontal rule
    if _re.fullmatch(r'[-*_]{3,}', stripped.strip()):
        return "[dim]" + "─" * 42 + "[/dim]", False
    # Headings
    if stripped.startswith("#### "):
        return pfx + "[bold dim]" + _md_inline_to_rich(stripped[5:]) + "[/bold dim]", False
    if stripped.startswith("### "):
        return pfx + "[bold]" + _md_inline_to_rich(stripped[4:]) + "[/bold]", False
    if stripped.startswith("## "):
        return pfx + "[bold underline]" + _md_inline_to_rich(stripped[3:]) + "[/bold underline]", False
    if stripped.startswith("# "):
        return pfx + "[bold underline bright_white]" + _md_inline_to_rich(stripped[2:]) + "[/bold underline bright_white]", False
    # Blockquote
    if stripped.startswith("> "):
        return pfx + "[dim italic]▎ " + _md_inline_to_rich(stripped[2:]) + "[/dim italic]", False
    # Unordered list
    if stripped.startswith(("- ", "* ", "+ ")):
        return pfx + "  • " + _md_inline_to_rich(stripped[2:]), False
    # Ordered list
    m = _re.match(r'^(\d+)\.\s+(.*)', stripped)
    if m:
        return pfx + f"  {m.group(1)}. " + _md_inline_to_rich(m.group(2)), False
    # Plain line
    return _md_inline_to_rich(raw_line), False


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


def _parse_hunk_header(line: str) -> tuple[int, int] | None:
    """Parse '@@ -old_start,.. +new_start,.. @@' and return (old_start, new_start)."""
    import re as _re2
    m = _re2.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _file_diff_text(file_path: str, is_new: bool, max_lines: int = 35, add_color: str = "green") -> list[str]:
    """Return markup lines for a file diff/preview with line numbers.

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
            old_num: int | None = None
            new_num: int | None = None
            for ln in hunk_lines:
                if ln.startswith("@@"):
                    parsed = _parse_hunk_header(ln)
                    if parsed:
                        old_num, new_num = parsed
                    # Show hunk header compactly (only the @@ part)
                    hdr = ln.split("@@")[1].strip() if ln.count("@@") >= 2 else ""
                    hdr_safe = hdr[:60].replace("[", "\\[") if hdr else ""
                    at_part = f"  [dim]@@  {hdr_safe}[/dim]" if hdr_safe else "  [dim]@@[/dim]"
                    lines.append(at_part)
                elif ln.startswith("+"):
                    num_pfx = f"[dim]{new_num:>4}[/dim] " if new_num is not None else "     "
                    safe = ln[1:].replace("[", "\\[")
                    lines.append(f"  {num_pfx}[{add_color}]+[/{add_color}] [{add_color}]{safe}[/{add_color}]")
                    if new_num is not None:
                        new_num += 1
                elif ln.startswith("-"):
                    num_pfx = f"[dim]{old_num:>4}[/dim] " if old_num is not None else "     "
                    safe = ln[1:].replace("[", "\\[")
                    lines.append(f"  {num_pfx}[red]-[/red] [red]{safe}[/red]")
                    if old_num is not None:
                        old_num += 1
                else:
                    # Context line
                    num_pfx = f"[dim]{new_num:>4}[/dim] " if new_num is not None else "     "
                    safe = ln.replace("[", "\\[")
                    lines.append(f"  {num_pfx}[dim]  {safe}[/dim]")
                    if new_num is not None:
                        new_num += 1
                    if old_num is not None:
                        old_num += 1
            if len(hunk_lines) == max_lines:
                lines.append("  [dim]… (truncated)[/dim]")
            return lines
        # no git diff — fall through to content

    try:
        content = path.read_text(errors="replace").splitlines()
        shown = content[:max_lines]
        for line_num, ln in enumerate(shown, start=1):
            safe = ln.replace("[", "\\[")
            num_pfx = f"[dim]{line_num:>4}[/dim] "
            if is_new:
                lines.append(f"  {num_pfx}[{add_color}]+ {safe}[/{add_color}]")
            else:
                lines.append(f"  {num_pfx}[dim]  {safe}[/dim]")
        if len(content) > max_lines:
            lines.append(f"  [dim]… ({len(content) - max_lines} more lines)[/dim]")
    except Exception:
        pass
    return lines


def create_textual_app(container, chat_id: int = 0):
    from cli.command_catalog import grouped_help_lines, quick_reference_commands, textual_command_map
    from cli.session_actions import (
        build_commit_message,
        clear_session_state,
        compact_session,
        render_todos_lines,
        render_usage_lines,
        run_git_commit,
        run_review_pass,
    )

    try:
        from textual.app import App, ComposeResult
        from textual.containers import Container, Horizontal, VerticalScroll
        from textual.reactive import reactive
        from textual.screen import ModalScreen
        from textual.suggester import Suggester
        from textual.widget import Widget
        from textual.widgets import Input, Label, Static
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError(
            "Textual mode requires the 'textual' package. Install it with './venv/bin/pip install textual'."
        ) from exc

    COMMANDS: dict[str, tuple[str, str]] = textual_command_map()

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

    class CommandDropdown(Static):
        """Floating dropdown showing slash command completions."""

        def __init__(self):
            super().__init__("", id="command-dropdown")
            self.matches: list[str] = []
            self.selected: int = 0

        def refresh_matches(self, partial: str) -> int:
            """Recompute matches for *partial* (starts with /).  Returns count."""
            low = partial.casefold()
            self.matches = [k for k in COMMANDS if k.casefold().startswith(low)]
            self.selected = min(self.selected, max(0, len(self.matches) - 1))
            self._render_list()
            return len(self.matches)

        def move(self, delta: int):
            if not self.matches:
                return
            self.selected = (self.selected + delta) % len(self.matches)
            self._render_list()

        def current(self) -> str | None:
            if not self.matches:
                return None
            return self.matches[self.selected]

        _WINDOW = 7  # items visible at once (leave 1 row for ▲/▼ each)

        def _render_list(self):
            total = len(self.matches)
            if not total:
                self.update("")
                return

            W = self._WINDOW
            # Slide the window so selected is always visible
            half = W // 2
            start = max(0, min(self.selected - half, total - W))
            end = min(total, start + W)

            lines = []
            if start > 0:
                lines.append(f"  [dim]  ▲  {start} more[/dim]")
            for i in range(start, end):
                name = self.matches[i]
                group, desc = COMMANDS[name]
                safe_desc = desc.replace("[", "\\[")
                safe_name = name.replace("[", "\\[")
                if i == self.selected:
                    lines.append(
                        f"  [bold reverse] ▶ {safe_name:<22}[/bold reverse]"
                        f"  [dim]{group} — {safe_desc}[/dim]"
                    )
                else:
                    lines.append(
                        f"  [dim]   {safe_name:<22}  {group} — {safe_desc}[/dim]"
                    )
            if end < total:
                lines.append(f"  [dim]  ▼  {total - end} more[/dim]")
            self.update("\n".join(lines))

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

    class AutoscrollIndicator(Static):
        pass

    class PasswordModal(ModalScreen):
        """Overlay dialog for entering a password that the running agent needs."""

        CSS = """
        PasswordModal {
            align: center middle;
        }
        #pw-dialog {
            width: 52;
            height: auto;
            border: round #555;
            background: #1e1e1e;
            padding: 1 2;
        }
        #pw-title {
            text-align: center;
            color: #d4d4d4;
            margin-bottom: 1;
        }
        #pw-hint {
            text-align: center;
            color: #666;
            margin-top: 1;
        }
        #pw-input {
            border: round #555;
            background: #2d2d2d;
            color: #d4d4d4;
            width: 100%;
        }
        #pw-input:focus {
            border: round #b07cff;
        }
        """

        def compose(self) -> ComposeResult:
            with Container(id="pw-dialog"):
                yield Label("🔑  Password required", id="pw-title")
                yield Input(password=True, placeholder="enter password…", id="pw-input")
                yield Label("Enter  to send  ·  Esc  to dismiss", id="pw-hint")

        def on_mount(self) -> None:
            self.query_one("#pw-input", Input).focus()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.dismiss(event.value)

        def on_key(self, event) -> None:
            if event.key == "escape":
                self.dismiss(None)

    class BridgeTextualApp(App):
        CSS = """
        Screen {
            background: #1e1e1e;
            color: #d4d4d4;
        }

        #titlebar {
            height: 1;
            padding: 0 2;
            background: #1e1e1e;
            color: #6a6a6a;
        }

        #workspace {
            margin: 0;
            height: 1fr;
        }

        #stream-scroll {
            height: 1fr;
            width: 1fr;
            border: none;
            scrollbar-size-vertical: 0;
            scrollbar-size-horizontal: 0;
            scrollbar-background: #1e1e1e;
            scrollbar-color: #1e1e1e;
        }

        #stream {
            padding: 0 2;
            height: auto;
            width: 100%;
        }

        #side {
            display: none;
            width: 0;
        }

        #statusline {
            height: 1;
            padding: 0 2;
            background: #252526;
            color: #5a5a5a;
        }

        #statusline.active {
            color: #d4d4d4;
        }

        #multiline-preview {
            height: auto;
            max-height: 6;
            padding: 0 2;
            background: #252526;
            color: #6a6a6a;
            display: none;
        }

        #multiline-preview.active {
            display: block;
        }

        #input {
            dock: bottom;
            margin: 0 1 0 1;
        }

        #search-bar {
            dock: bottom;
            margin: 0 1 0 1;
            border: solid #b07cff;
            display: none;
        }

        #search-bar.active {
            display: block;
        }

        #autoscroll-indicator {
            height: 1;
            padding: 0 2;
            background: #252526;
            color: #5a5a5a;
            display: none;
        }

        #autoscroll-indicator.active {
            display: block;
        }

        #command-dropdown {
            dock: bottom;
            margin: 0 1 0 1;
            height: auto;
            max-height: 11;
            background: #252526;
            border: round #3a3a3a;
            padding: 0 0;
            display: none;
            overflow-y: hidden;
            overflow-x: hidden;
        }

        #command-dropdown.active {
            display: block;
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
            self._autoscroll: bool = True  # auto-scroll to bottom on new content
            self._search_active: bool = False  # Ctrl+F search mode
            self._search_snapshot: list[str] = []  # stream snapshot before search
            self._task_start_time: float = 0.0  # for notify-send on long tasks
            self._streamed_text_start: int | None = None  # index of first 💬 line in current run
            self._active_task: asyncio.Task | None = None  # running prompt / orchestration task
            # Operation indicator state — groups consecutive read/write events into one line
            self._op_type: str = ""           # "reading" | "writing" | "running" | ""
            self._op_files: list[str] = []    # filenames accumulated for current op group
            self._op_line_idx: int = -1       # index in _stream_lines of the op indicator line
            # Streaming text render state
            self._text_buffer: str = ""       # incomplete current line accumulator
            self._text_in_code: bool = False  # inside a ``` code fence?
            self._text_has_partial: bool = False  # is the last _stream_lines entry an unfinished partial?
            self._awaiting_plan_confirm: bool = False  # True after /plan — next Enter runs or cancels
            self._password_modal_open: bool = False  # True while PasswordModal is on screen

        def watch_current_provider(self, _old: str, _new: str) -> None:
            """Refresh the idle status bar whenever the active provider changes."""
            if self._status_timer is None:
                try:
                    self.query_one("#statusline", StatusLineWidget).update(self._idle_status_renderable())
                except Exception:
                    pass

        def _provider_color(self) -> str:
            mapping = {
                "qwen": "#b07cff",
                "codex": "#6aa7ff",
                "claude": "#ff9e57",
            }
            return mapping.get(self.current_provider, "#6aa7ff")

        def _dim_stream_history(self):
            """Dim all existing stream lines when a new session begins."""
            dimmed = []
            for line in self._stream_lines:
                if not line.strip():
                    dimmed.append(line)
                elif line.startswith("[dim]") and line.endswith("[/dim]"):
                    dimmed.append(line)
                else:
                    dimmed.append(f"[dim]{line}[/dim]")
            self._stream_lines = dimmed

        @staticmethod
        def _plan_panel_lines(strategy: str, complexity: str, eta: str, rationale: str, width: int = 88) -> list[str]:
            """Render an AI Plan bordered panel as Rich markup lines."""
            label = " AI Plan "
            inner = width - 2
            dash_l = (inner - len(label)) // 2
            dash_r = inner - len(label) - dash_l
            top = f"[dim]╭{'─' * dash_l}[/dim][cyan]{label}[/cyan][dim]{'─' * dash_r}╮[/dim]"
            bot = f"[dim]╰{'─' * inner}╯[/dim]"
            pad = inner - 2  # usable chars inside "│  … │"

            def row(lbl: str, val: str) -> list[str]:
                lw = 11
                prefix = f"  [bold dim]{lbl:<{lw}}[/bold dim] "
                prefix_plain_len = lw + 3  # "  label  "
                available = pad - prefix_plain_len
                # simple wrap at available chars (plain text estimate)
                import re as _r
                plain_val = _r.sub(r"\[.*?\]", "", val)
                if len(plain_val) <= available:
                    content = f"{prefix}{val}"
                    right_pad = " " * max(0, pad - prefix_plain_len - len(plain_val))
                    return [f"[dim]│[/dim]{content}{right_pad}[dim]│[/dim]"]
                # wrap
                words = plain_val.split()
                out_rows = []
                line_words: list[str] = []
                for w in words:
                    if sum(len(x) + 1 for x in line_words) + len(w) > available:
                        out_rows.append(" ".join(line_words))
                        line_words = [w]
                    else:
                        line_words.append(w)
                if line_words:
                    out_rows.append(" ".join(line_words))
                result = []
                for idx, r in enumerate(out_rows):
                    rpad = " " * max(0, pad - prefix_plain_len - len(r))
                    if idx == 0:
                        result.append(f"[dim]│[/dim]{prefix}{r}{rpad}[dim]│[/dim]")
                    else:
                        cont_prefix = "  " + " " * (lw + 2)
                        result.append(f"[dim]│[/dim]{cont_prefix}{r}{rpad}[dim]│[/dim]")
                return result

            lines = ["", top]
            lines.extend(row("strategy", strategy))
            comp_val = f"{complexity}  [dim]·[/dim]  ETA {eta}"
            lines.extend(row("complexity", comp_val))
            if rationale:
                lines.append(f"[dim]│[/dim]{'':>{inner}}[dim]│[/dim]".replace(f"{'':>{inner}}", " " * inner))
                lines.extend(row("rationale", rationale))
            lines.append(bot)
            return lines

        @staticmethod
        def _subtask_table_lines(subtasks: list, prov_colors: dict) -> list[str]:
            """Render subtask list as an aligned table with colored providers."""
            COL_ID = 6
            COL_TITLE = 30
            COL_PROV = 10
            COL_DEP = 12
            header = (
                f"  [bold dim]{'#':<{COL_ID}}{'subtask':<{COL_TITLE}}"
                f"{'provider':<{COL_PROV}}{'depends on':<{COL_DEP}}group[/bold dim]"
            )
            sep = f"  [dim]{'─' * (COL_ID + COL_TITLE + COL_PROV + COL_DEP + 8)}[/dim]"
            lines = ["", header, sep]
            for item in subtasks:
                color = prov_colors.get(item.suggested_provider, "#ffffff")
                sid = item.subtask_id[:5]  # keep short for alignment
                title = item.title
                dep = ", ".join(item.depends_on) if item.depends_on else "—"
                group = str(item.parallel_group)
                # title may wrap
                if len(title) <= COL_TITLE - 1:
                    lines.append(
                        f"  [dim]{sid:<{COL_ID}}[/dim]{title:<{COL_TITLE}}"
                        f"[{color}]{item.suggested_provider:<{COL_PROV}}[/{color}]"
                        f"[dim]{dep:<{COL_DEP}}{group}[/dim]"
                    )
                else:
                    first = title[: COL_TITLE - 1]
                    rest = title[COL_TITLE - 1 :]
                    lines.append(
                        f"  [dim]{sid:<{COL_ID}}[/dim]{first:<{COL_TITLE}}"
                        f"[{color}]{item.suggested_provider:<{COL_PROV}}[/{color}]"
                        f"[dim]{dep:<{COL_DEP}}{group}[/dim]"
                    )
                    lines.append(f"  {' ' * COL_ID}[dim]{rest}[/dim]")
            return lines

        def compose(self) -> ComposeResult:
            session = container.get_session(chat_id)
            self.current_provider = session.current_provider
            yield TitleWidget(self._titlebar_text(), id="titlebar")
            with Container(id="workspace"):
                with VerticalScroll(id="stream-scroll"):
                    yield StreamWidget("", id="stream")
                yield SideWidget("", id="side")  # hidden via CSS; kept for compat
            yield StatusLineWidget("", id="statusline")
            yield AutoscrollIndicator("", id="autoscroll-indicator")
            yield MultilinePreview("", id="multiline-preview")
            yield CommandDropdown()
            yield Input(
                placeholder="/help · @provider:prompt · @file · Shift+Enter multiline · Ctrl+F search",
                id="input",
                suggester=SlashCommandSuggester(),
            )
            yield Input(placeholder="Search stream…  Esc to close", id="search-bar")

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

        def _probe_provider_status(self, name: str, cli_path: str) -> tuple[str, str]:
            """Returns (label, hex_color) for a provider.
            Fast synchronous check — no subprocess, no blocking I/O beyond a small JSON read.
            Priority: runtime.health (authoritative) → static probe (binary + auth files).
            """
            import shutil as _sh, json as _pj, time as _pt, os as _po
            from pathlib import Path as _PP

            session = container.get_session(chat_id)
            runtime = session.runtimes.get(name)

            # ── Runtime health (from actual API usage) ────────────────────────
            if runtime is not None and runtime.health is not None:
                h = runtime.health
                if h.is_available_now():
                    fails = f"  {h.consecutive_failures}✗" if h.consecutive_failures else ""
                    return f"up{fails}", "#44dd88"
                reason = h.last_failure.short_label if h.last_failure else "unavailable"
                ri = h.retry_in_seconds
                if ri and ri > 0:
                    m, s = divmod(ri, 60)
                    timer = f"{m}m" if m else f"{s}s"
                    return f"{reason}  retry {timer}", "#ff6666"
                return reason, "#ff6666"

            # ── Static probe (no runtime yet) ─────────────────────────────────
            home = _PP.home()

            # 1. Binary installed?
            if not _sh.which(cli_path) and not _PP(cli_path).is_file():
                return "not installed", "#666666"

            # 2. Auth / credentials check (provider-specific)
            if name == "qwen":
                creds_file = home / ".qwen" / "oauth_creds.json"
                if not creds_file.exists():
                    return "no auth  (run: qwen login)", "#ffaa44"
                try:
                    creds = _pj.loads(creds_file.read_text())
                    exp_ms = float(creds.get("expiry_date", 0))
                    if exp_ms:
                        remaining = exp_ms / 1000 - _pt.time()
                        if remaining <= 0:
                            return "token expired", "#ff6666"
                        h_left = int(remaining / 3600)
                        m_left = int((remaining % 3600) / 60)
                        if remaining < 600:          # < 10 min — urgent
                            return f"token ~{m_left}m left", "#ff9944"
                        if remaining < 3600:         # < 1h — warn
                            return f"ready  (~{m_left}m)", "#ffcc44"
                        return f"ready  (~{h_left}h)", "#44dd88"
                    return "ready", "#44dd88"
                except Exception as exc:
                    return f"auth err: {exc}", "#ffaa44"

            elif name == "codex":
                # Codex uses ChatGPT OAuth stored in ~/.codex/auth.json
                auth_file = home / ".codex" / "auth.json"
                if auth_file.exists():
                    try:
                        auth = _pj.loads(auth_file.read_text())
                        tokens = auth.get("tokens", {})
                        if tokens.get("access_token") or tokens.get("id_token") or auth.get("OPENAI_API_KEY"):
                            return "ready", "#44dd88"
                        return "no token", "#ffaa44"
                    except Exception:
                        return "auth err", "#ffaa44"
                # Fallback: env var
                if _po.getenv("OPENAI_API_KEY"):
                    return "ready  (env key)", "#44dd88"
                return "no auth  (run: codex login)", "#ffaa44"

            elif name == "claude":
                # Claude Code authenticates via claude.ai OAuth
                # Active sessions are stored in ~/.claude/sessions/
                sessions_dir = home / ".claude" / "sessions"
                if sessions_dir.exists():
                    session_files = list(sessions_dir.iterdir())
                    if session_files:
                        return "ready", "#44dd88"
                # Check ANTHROPIC_API_KEY as fallback
                if _po.getenv("ANTHROPIC_API_KEY"):
                    return "ready  (env key)", "#44dd88"
                # Settings exist → installed but maybe no active session
                if (home / ".claude" / "settings.json").exists():
                    return "no session  (run: claude login)", "#ffaa44"
                return "no auth", "#ffaa44"

            # Unknown provider — binary is there, assume OK
            return "ready", "#44dd88"

        def _welcome_text(self) -> str:
            session = container.get_session(chat_id)
            cwd = str(session.file_mgr.get_working_dir())
            prov = self.current_provider
            color = self._provider_color()
            git = _git_status_short(cwd)

            SEP = "  [dim]" + "─  " * 28 + "[/dim]"

            import pathlib
            p = pathlib.Path(cwd)
            try:
                short_cwd = "…/" + "/".join(p.parts[-2:])
            except Exception:
                short_cwd = cwd
            git_part = f"  [dim]{git}[/dim]" if git else ""

            # ── Header ────────────────────────────────────────────────────────
            header_lines = [
                "",
            ]

            # ── Providers ─────────────────────────────────────────────────────
            prov_colors = {"qwen": "#b07cff", "codex": "#6aa7ff", "claude": "#ff9e57"}
            prov_lines = [SEP, ""]
            for pname, pcli in container.provider_paths.items():
                label, col = self._probe_provider_status(pname, pcli)
                pcol = prov_colors.get(pname, color)
                model_tag = session.provider_models.get(pname, "")
                model_str = f"  [dim]{model_tag}[/dim]" if model_tag else ""
                prov_lines.append(
                    f"  [{pcol}]{pname:<8}[/{pcol}]  [{col}]{label}[/{col}]{model_str}"
                )
            prov_lines.append("")

            # ── Recent runs ───────────────────────────────────────────────────
            recent = container.recent_runs(session, limit=5)
            if recent:
                recent_lines = []
                for run in recent:
                    pcol = prov_colors.get(run.provider_summary or "", color)
                    prompt_hint = f"  [dim]{(run.prompt or '')[:48]}[/dim]" if getattr(run, "prompt", "") else ""
                    recent_lines.append(
                        f"  {run.status_emoji}  [dim]{run.mode:<10}[/dim]"
                        f"  [{pcol}]{run.provider_summary or 'mixed'}[/{pcol}]"
                        f"{prompt_hint}"
                    )
            else:
                recent_lines = ["  [dim]No runs yet — send a prompt to get started.[/dim]"]

            # ── Commands ──────────────────────────────────────────────────────
            cmd_cols = quick_reference_commands()
            cmd_lines = []
            for i in range(0, len(cmd_cols), 2):
                left = cmd_cols[i]
                right = cmd_cols[i + 1] if i + 1 < len(cmd_cols) else None
                lstr = f"  [{color}]{left[0]:<18}[/{color}][dim]{left[1]}[/dim]"
                rstr = f"  [{color}]{right[0]:<18}[/{color}][dim]{right[1]}[/dim]" if right else ""
                cmd_lines.append(lstr + rstr)

            # ── Assemble ──────────────────────────────────────────────────────
            parts: list[str] = [
                *header_lines,
                *prov_lines,
                SEP,
                "",
                f"  [bold dim]Recent runs[/bold dim]",
                *recent_lines,
                "",
                SEP,
                "",
                f"  [bold dim]Commands[/bold dim]  [dim](type / to open dropdown)[/dim]",
                *cmd_lines,
                "",
                SEP,
                f"  [dim][bold]/[/bold] command menu   "
                f"[bold]Tab[/bold] autocomplete   "
                f"[bold]@file.py[/bold] inline file   "
                f"[bold]@provider:[/bold]prompt   "
                f"[bold]Shift+Enter[/bold] multi-line   "
                f"[bold]Ctrl+F[/bold] search[/dim]",
                "",
            ]
            return "\n".join(parts)

        def on_mount(self):
            self._load_history()
            self._apply_provider_theme()
            self._refresh_all()
            self._set_stream(self._welcome_text())
            # Populate the always-visible bottom info bar
            self.query_one("#statusline", StatusLineWidget).update(self._idle_status_renderable())
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
            # Keep idle bar fresh (git branch / ctx may have changed)
            if self._status_timer is None:
                self.query_one("#statusline", StatusLineWidget).update(self._idle_status_renderable())

        def _titlebar_text(self) -> str:
            session = container.get_session(chat_id)
            cwd = str(session.file_mgr.get_working_dir())
            git = _git_status_short(cwd)
            ctx_tok = self._ctx_input_tokens
            import pathlib
            p = pathlib.Path(cwd)
            short_cwd = p.name or cwd
            color = self._provider_color()
            suffix_parts = [f"{self.current_provider}  ·  {short_cwd}"]
            if git:
                suffix_parts.append(git)
            if self.current_mode != "idle":
                suffix_parts.append(self.current_mode)
            if self.remote_state != "stopped":
                suffix_parts.append(f"remote:{self.remote_state}")
            suffix = "  ·  ".join(suffix_parts)
            return f"  [{color}]◆[/{color}] [bold white]Forge[/bold white]  [dim]v0.1  ·  {suffix}[/dim]"

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
            for provider_name, pcli in container.provider_paths.items():
                label, _ = self._probe_provider_status(provider_name, pcli)
                provider_lines.append(f"{provider_name}: {label}")
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
            # Only the input box gets the provider accent border; stream area is borderless
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
            frame = self._status_state.get("frame", 0)
            spinner = _SPIN_FRAMES[frame % len(_SPIN_FRAMES)]
            dot = _DOT_FRAMES[frame % len(_DOT_FRAMES)]
            # Strip any static trailing ellipsis/dots from the action label —
            # the animated dot suffix takes over.
            action = self._status_state["action"].rstrip("…·. ")
            # Show model name next to provider when available
            session = container.get_session(chat_id)
            provider = self.current_provider
            model_str = session.provider_models.get(provider, "")
            if not model_str:
                runtime = session.runtimes.get(provider)
                model_str = (runtime.manager.model_name if runtime and hasattr(runtime, "manager") else "") or ""
            model_part = f"  [dim]{model_str}[/dim]" if model_str else ""
            return f"[{color}]{spinner}[/] {action}[dim]{dot}[/dim]{model_part}  [dim]({time_str} · {tok_str} tokens)[/dim]"

        def _idle_status_renderable(self) -> str:
            session = container.get_session(chat_id)
            cwd = str(session.file_mgr.get_working_dir())
            from pathlib import Path as _PL
            home = str(_PL.home())
            basename = cwd.replace(home, "~", 1) if cwd.startswith(home) else cwd
            git = _git_status_short(cwd)
            git_part = f"  [{git}]" if git else ""
            provider = self.current_provider
            color = self._provider_color()
            # Model name: prefer explicit override, then runtime's actual model
            model_str = session.provider_models.get(provider, "")
            if not model_str:
                runtime = session.runtimes.get(provider)
                model_str = (runtime.manager.model_name if runtime and hasattr(runtime, "manager") else "") or ""
            model_part = f"  [dim]·[/dim]  {model_str}" if model_str else ""
            ctx = self._ctx_input_tokens
            ctx_part = f"  [dim]·[/dim]  ctx {ctx // 1000}k" if ctx >= 1000 else (f"  [dim]·[/dim]  ctx {ctx}" if ctx else "")
            return f"[{color}]◆[/{color}]  {provider}{model_part}  [dim]·[/dim]  {basename}{git_part}{ctx_part}"

        def _show_status_line(self, provider: str | None = None):
            self._status_state["start"] = _time.monotonic()
            sl = self.query_one("#statusline", StatusLineWidget)
            sl.add_class("active")
            sl.update(self._status_renderable())
            if self._status_timer is not None:
                self._status_timer.stop()
            self._status_state["frame"] = 0
            self._status_timer = self.set_interval(0.12, self._tick_status)

        def _tick_status(self):
            self._status_state["frame"] = self._status_state.get("frame", 0) + 1
            sl = self.query_one("#statusline", StatusLineWidget)
            sl.update(self._status_renderable())

        def _hide_status_line(self):
            if self._status_timer is not None:
                self._status_timer.stop()
                self._status_timer = None
            sl = self.query_one("#statusline", StatusLineWidget)
            sl.remove_class("active")
            sl.update(self._idle_status_renderable())

        def _open_password_dialog(self) -> None:
            """Open the password modal if an agent is running and the modal isn't already shown."""
            if self._password_modal_open:
                return
            runtime = self._active_runtime
            if runtime is None:
                self._append_stream("  [dim]No active agent to send password to.[/dim]")
                return

            self._password_modal_open = True

            def _on_dismiss(password: str | None) -> None:
                self._password_modal_open = False
                if password is None:
                    self._append_stream("  [dim]Password dismissed.[/dim]")
                    return
                async def _send():
                    ok = await runtime.manager.write_stdin(password)
                    if ok:
                        self._append_stream("  [dim]🔑 Password sent.[/dim]")
                    else:
                        self._append_stream("  [red]Failed to send password — agent stdin not available.[/red]")
                asyncio.create_task(_send())

            self.push_screen(PasswordModal(), _on_dismiss)

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
            if not self._autoscroll:
                return
            scroll = self.query_one("#stream-scroll", VerticalScroll)
            scroll.scroll_end(animate=False)

        def _toggle_autoscroll(self):
            self._autoscroll = not self._autoscroll
            ind = self.query_one("#autoscroll-indicator", AutoscrollIndicator)
            if self._autoscroll:
                ind.remove_class("active")
                ind.update("")
                self._scroll_to_bottom()
            else:
                ind.add_class("active")
                ind.update("  [dim]autoscroll OFF — press [bold]A[/bold] to resume[/dim]")

        def _open_search(self):
            self._search_active = True
            self._search_snapshot = list(self._stream_lines)
            sb = self.query_one("#search-bar", Input)
            sb.value = ""
            sb.add_class("active")
            sb.focus()

        def _close_search(self):
            self._search_active = False
            sb = self.query_one("#search-bar", Input)
            sb.remove_class("active")
            # Restore full stream
            self._stream_lines = self._search_snapshot
            self.query_one("#stream", StreamWidget).update("\n".join(self._stream_lines))
            self.call_after_refresh(self._scroll_to_bottom)
            self.query_one("#input", Input).focus()

        def _apply_search(self, term: str):
            if not term:
                lines = self._search_snapshot
            else:
                t = term.lower()
                lines = [l for l in self._search_snapshot if t in l.lower()]
                if not lines:
                    lines = [f"  [dim]No matches for '{term}'[/dim]"]
            self.query_one("#stream", StreamWidget).update("\n".join(lines))

        def _set_stream(self, content: str):
            """Replace stream content and sync _stream_lines."""
            self._stream_lines = content.splitlines() if content else []
            self._last_stream_was_text = False
            self.query_one("#stream", StreamWidget).update(content)
            self.call_after_refresh(self._scroll_to_bottom)

        def _push_output(self, content: str):
            """Append command output below existing stream content with a separator.

            This keeps the welcome screen (or prior output) visible above while
            showing the new result below — matching the terminal-history model
            used by Claude Code and Qwen CLI.
            """
            sep = "  [dim]" + "─  " * 28 + "[/dim]"
            if self._stream_lines:
                self._append_stream("", sep, *content.splitlines())
            else:
                self._set_stream(content)

        def _append_stream(self, *lines: str):
            self._last_stream_was_text = False
            self._stream_lines.extend(line for line in lines if line is not None)
            self._stream_lines = self._stream_lines[-5000:]
            self.query_one("#stream", StreamWidget).update("\n".join(self._stream_lines))
            self.call_after_refresh(self._scroll_to_bottom)

        def _op_indicator_text(self, op_type: str, files: list[str], color: str) -> str:
            """Render the 'reading N file(s) · name' indicator line."""
            n = len(files)
            noun = f"{n} file" if n == 1 else f"{n} files"
            # Show up to 3 filenames inline
            names = ", ".join(Path(f).name for f in files[:3])
            if len(files) > 3:
                names += f", +{len(files) - 3} more"
            if op_type == "reading":
                icon = "[dim]↳[/dim]"
                action = f"[dim]reading {noun}[/dim]"
            elif op_type == "writing":
                icon = f"[{color}]↳[/{color}]"
                action = f"[{color}]writing {noun}[/{color}]"
            else:
                icon = "[dim]↳[/dim]"
                action = f"[dim]{op_type}[/dim]"
            return f"  {icon} {action}  [dim]{names}[/dim]"

        def _append_stream_event(self, line: str):
            """Format and append a stream event line to the stream widget."""
            color = self._provider_color()
            if line.startswith("💬 "):
                # Auto-open password dialog if the agent is asking for a password
                if _PASSWORD_RE.search(line[2:]) and not self._password_modal_open:
                    self.call_after_refresh(self._open_password_dialog)
                # Any text chunk resets the op group (but does NOT remove the op indicator line)
                self._op_type = ""
                self._op_files = []
                self._op_line_idx = -1

                chunk = line[2:]  # strip "💬 " prefix (emoji + space = 2 chars)

                if self._streamed_text_start is None:
                    self._streamed_text_start = len(self._stream_lines)

                # Accumulate chunk; split off complete lines
                self._text_buffer += chunk
                parts = self._text_buffer.split("\n")
                complete_lines, self._text_buffer = parts[:-1], parts[-1]

                # Replace the existing partial preview with the first complete line
                if self._text_has_partial and complete_lines and self._stream_lines:
                    first_rendered, self._text_in_code = _render_stream_line(complete_lines[0], self._text_in_code)
                    self._stream_lines[-1] = first_rendered
                    complete_lines = complete_lines[1:]
                    self._text_has_partial = False

                # Append remaining complete lines
                for raw_line in complete_lines:
                    rendered, self._text_in_code = _render_stream_line(raw_line, self._text_in_code)
                    self._stream_lines.append(rendered)

                # Show/update partial buffer as a live preview at the very end
                if self._text_buffer:
                    partial, _ = _render_stream_line(self._text_buffer, self._text_in_code)
                    if self._text_has_partial and self._stream_lines:
                        self._stream_lines[-1] = partial  # update existing partial in-place
                    else:
                        self._stream_lines.append(partial)
                        self._text_has_partial = True
                else:
                    self._text_has_partial = False

                self._last_stream_was_text = False
                self._stream_lines = self._stream_lines[-5000:]
                self.query_one("#stream", StreamWidget).update("\n".join(self._stream_lines))
                self.call_after_refresh(self._scroll_to_bottom)
                return

            # Any non-text event ends the current text run
            self._last_stream_was_text = False

            # ---- Read events: group into a single updating indicator line ----
            if line.startswith("👁️ "):
                raw = line.split(None, 1)[-1].strip() if " " in line else ""
                # Extract the last token as filename (events may contain extra words)
                fname = raw.split()[-1] if raw.split() else raw
                self._flush_op_if_changed("reading")
                self._op_files.append(fname)
                self._upsert_op_line(color)
                return

            # ---- Write events: same grouping ----
            if line.startswith(("✏️ ", "📂 ")):
                raw = line.split(None, 1)[-1].strip() if " " in line else ""
                fname = raw.split()[-1] if raw.split() else raw
                self._flush_op_if_changed("writing")
                self._op_files.append(fname)
                self._upsert_op_line(color)
                return

            # Any other event type ends the current read/write group
            self._op_type = ""
            self._op_files = []
            self._op_line_idx = -1

            if line.startswith("🔧 Использую: ") or line.startswith("🔧 "):
                tool = line.split(": ", 1)[-1].strip() if ": " in line else line[2:].strip()
                formatted = f"  [{color}]✦[/] [dim]{tool.replace('[', chr(92) + '[')}[/dim]"
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
            self._stream_lines = self._stream_lines[-5000:]
            self.query_one("#stream", StreamWidget).update("\n".join(self._stream_lines))
            self.call_after_refresh(self._scroll_to_bottom)

        def _flush_op_if_changed(self, new_op: str):
            """If the op type changed, finalize the old group and start fresh."""
            if self._op_type and self._op_type != new_op:
                self._op_type = ""
                self._op_files = []
                self._op_line_idx = -1
            self._op_type = new_op

        def _upsert_op_line(self, color: str):
            """Create or update the operation indicator line in-place."""
            text = self._op_indicator_text(self._op_type, self._op_files, color)
            if self._op_line_idx >= 0 and self._op_line_idx < len(self._stream_lines):
                # Update the existing indicator line in-place
                self._stream_lines[self._op_line_idx] = text
            else:
                # Append a new indicator line and record its index
                self._stream_lines.append(text)
                self._op_line_idx = len(self._stream_lines) - 1
            self._stream_lines = self._stream_lines[-5000:]
            # After truncation the index may have shifted — recalculate
            if self._op_line_idx >= 0:
                self._op_line_idx = max(0, self._op_line_idx - max(0, len(self._stream_lines) - 300))
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

        # on_input_changed is merged below into the async handler

        def _dropdown_active(self) -> bool:
            try:
                return "active" in self.query_one("#command-dropdown", CommandDropdown).classes
            except Exception:
                return False

        def _dropdown_complete(self):
            """Fill input with currently selected dropdown command."""
            dd = self.query_one("#command-dropdown", CommandDropdown)
            chosen = dd.current()
            if chosen:
                inp = self.query_one("#input", Input)
                inp.value = chosen + " "
                inp.cursor_position = len(inp.value)
            dd.remove_class("active")

        async def on_key(self, event) -> None:
            # Dropdown navigation — intercept before other handlers
            if self._dropdown_active():
                dd = self.query_one("#command-dropdown", CommandDropdown)
                if event.key in ("tab", "down"):
                    dd.move(1)
                    event.prevent_default()
                    return
                if event.key == "up":
                    dd.move(-1)
                    event.prevent_default()
                    return
                if event.key in ("enter", "return"):
                    self._dropdown_complete()
                    event.prevent_default()
                    return
                if event.key == "escape":
                    dd.remove_class("active")
                    event.prevent_default()
                    return

            if event.key == "ctrl+f":
                if self._search_active:
                    self._close_search()
                else:
                    self._open_search()
                event.prevent_default()
                return
            if event.key == "a" and not self._search_active:
                # Only toggle when input widget is NOT focused
                try:
                    focused = self.focused
                    if focused is None or not isinstance(focused, Input):
                        self._toggle_autoscroll()
                        event.prevent_default()
                        return
                except Exception:
                    pass
            if event.key == "?":
                await self._handle_command("/help")
                return
            if event.key == "escape":
                if self._search_active:
                    self._close_search()
                    event.prevent_default()
                    return
                try:
                    dd = self.query_one("#command-dropdown", CommandDropdown)
                    if "active" in dd.classes:
                        dd.remove_class("active")
                        event.prevent_default()
                        return
                except Exception:
                    pass
                if self._multiline_buffer:
                    self._multiline_buffer = []
                    self._update_multiline_preview()
                    event.prevent_default()
                    return
                # Cancel active prompt / orchestration task
                if self._active_task is not None and not self._active_task.done():
                    self._active_task.cancel()
                    self._active_task = None
                    self._append_stream("  [yellow]↩ Cancelled (Esc)[/yellow]")
                    self._hide_status_line()
                    self.current_mode = "idle"
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
            if event.key == "ctrl+r":
                # Ctrl+R: reverse history search — show matching entries in stream
                input_widget = self.query_one("#input", Input)
                term = input_widget.value.strip().lower()
                matches = [
                    p for p in reversed(self._input_history)
                    if not term or term in p.lower()
                ][:10]
                if matches:
                    self._append_stream(
                        "",
                        "  [dim]── history search (type to filter, ↑/↓ to select) ──[/dim]",
                        *[f"  [dim]{i+1}.[/dim] {m.replace('[', '\\[')}" for i, m in enumerate(matches)],
                    )
                else:
                    self._append_stream("  [dim]No history matches.[/dim]")
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
            if event.key == "ctrl+p":
                self._open_password_dialog()
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

        def _update_dropdown(self, value: str):
            dd = self.query_one("#command-dropdown", CommandDropdown)
            if value.startswith("/") and " " not in value.strip():
                count = dd.refresh_matches(value)
                if count > 0:
                    dd.add_class("active")
                else:
                    dd.remove_class("active")
            else:
                dd.remove_class("active")

        async def on_input_changed(self, event: Input.Changed) -> None:
            if self._search_active and event.input.id == "search-bar":
                self._apply_search(event.value)
                return
            if event.input.id != "input":
                return
            self.current_input = event.value
            self._update_dropdown(event.value)
            self.query_one("#side", SideWidget).update(self._side_text())

        async def on_input_submitted(self, event: Input.Submitted):
            # Search bar Enter closes search
            if event.input.id == "search-bar":
                self._close_search()
                return
            # Close dropdown on submit
            try:
                self.query_one("#command-dropdown", CommandDropdown).remove_class("active")
            except Exception:
                pass
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

            # Y/N plan confirmation
            if self._awaiting_plan_confirm:
                self._awaiting_plan_confirm = False
                self.query_one("#input", Input).placeholder = "/help · @provider:prompt · @file · Shift+Enter multiline · Ctrl+F search"
                answer = value.lower().strip()
                if answer in ("y", "yes", ""):
                    session = container.get_session(chat_id)
                    plan = session.last_plan
                    if plan is not None:
                        if self._active_task is not None and not self._active_task.done():
                            self._append_stream("  [dim]⏳ Task running — press Esc to cancel first[/dim]")
                            return
                        self._active_task = asyncio.create_task(
                            self._run_orchestration(plan.prompt, prebuilt_plan=plan)
                        )
                    return
                else:
                    self._append_stream("  [dim]Plan cancelled.[/dim]")
                    return

            if value.startswith("/"):
                await self._handle_command(value)
                return

            # Block new AI prompts while a task is already running
            if self._active_task is not None and not self._active_task.done():
                self._append_stream(
                    "  [dim]⏳ Task running — press [bold]Esc[/bold] to cancel, or wait[/dim]"
                )
                return

            # @provider: prefix — run with a specific provider without permanently switching
            provider_override: str | None = None
            m_prov = _re.match(r'^@(qwen|codex|claude):\s*', value, _re.IGNORECASE)
            if m_prov:
                from providers import normalize_provider_name, is_supported_provider
                candidate = normalize_provider_name(m_prov.group(1))
                if is_supported_provider(candidate):
                    provider_override = candidate
                    value = value[m_prov.end():]

            self._active_task = asyncio.create_task(
                self._run_prompt(value, provider_override=provider_override)
            )

        async def _build_plan_ai(self, session, prompt: str, stream_event_callback=None):
            """Try AI planner first; fall back to rule-based silently."""
            planner = container.build_ai_planner(session)
            planning_provider = container.pick_planning_provider(session)
            planning_runtime = await container.ensure_runtime_started(session, planning_provider)
            return await planner.build_plan(
                prompt,
                container.execution_service,
                session,
                planning_runtime,
                stream_event_callback=stream_event_callback,
            )

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
                self._awaiting_plan_confirm = False
                self.query_one("#input", Input).placeholder = "/help · @provider:prompt · @file · Shift+Enter multiline · Ctrl+F search"
                self._set_orchestration_steps([])
                self._add_timeline("Workspace reset.")
                color = self._provider_color()
                self._append_stream("", f"  [{color}]◆[/{color}] [dim]new session[/dim]")
                self._dim_stream_history()
                self._push_output(self._welcome_text())
                self._refresh_all()
                return
            if command == "/cls":
                self._set_stream("")
                self._awaiting_plan_confirm = False
                self.query_one("#input", Input).placeholder = "/help · @provider:prompt · @file · Shift+Enter multiline · Ctrl+F search"
                self._add_timeline("Stream cleared.")
                return
            if command == "/clear":
                msg = clear_session_state(session, container)
                self._set_stream("")
                self._set_orchestration_steps([])
                self._add_timeline(msg)
                self._push_output(self._welcome_text())
                self._append_stream(f"  [green]✓[/green] {msg}")
                self._refresh_all()
                return
            if command == "/commands":
                self._push_output("\n".join(grouped_help_lines()))
                return
            if command == "/save":
                last = session.last_task_result
                if not last or not last.answer_text.strip():
                    self._push_output("[dim]Nothing to save yet.[/dim]")
                    return
                cwd = str(session.file_mgr.get_working_dir())
                fname = arg or f"answer_{_time.strftime('%Y%m%d_%H%M%S')}.md"
                save_path = Path(cwd) / fname
                try:
                    save_path.write_text(last.answer_text, encoding="utf-8")
                    self._add_timeline(f"Saved to {fname}")
                    self._append_stream(f"  [green]✓[/green] Saved to [bold]{save_path}[/bold]")
                except Exception as exc:
                    self._append_stream(f"  [red]Save failed: {exc}[/red]")
                return
            if command == "/export":
                fmt = (arg or "md").lower().strip(".")
                cwd = str(session.file_mgr.get_working_dir())
                fname = f"stream_{_time.strftime('%Y%m%d_%H%M%S')}.{fmt}"
                export_path = Path(cwd) / fname
                try:
                    # Strip Rich markup for plain text
                    import re as _re2
                    raw_content = "\n".join(self._stream_lines)
                    if fmt == "txt":
                        raw_content = _re2.sub(r'\[/?[^\]]+\]', '', raw_content)
                    export_path.write_text(raw_content, encoding="utf-8")
                    self._add_timeline(f"Exported to {fname}")
                    self._append_stream(f"  [green]✓[/green] Exported to [bold]{export_path}[/bold]")
                except Exception as exc:
                    self._append_stream(f"  [red]Export failed: {exc}[/red]")
                return
            if command == "/stats":
                stats = session.provider_stats
                if not stats:
                    self._push_output("[dim]No stats yet — run some tasks first.[/dim]")
                    return
                COLORS = {"qwen": "#b07cff", "codex": "#6aa7ff", "claude": "#ff9e57"}
                lines = ["[bold]Provider stats[/bold]", ""]
                for prov, s in sorted(stats.items()):
                    c = COLORS.get(prov, "#6aa7ff")
                    avg_s = f"{s.avg_ms / 1000:.1f}s" if s.avg_ms else "?"
                    rate = f"{s.success_rate * 100:.0f}%"
                    lines.append(
                        f"  [{c}]{prov}[/]  tasks={s.total_tasks}  "
                        f"ok={s.successful_tasks}  fail={s.failed_tasks}  "
                        f"retries={s.retry_count}  avg={avg_s}  success={rate}"
                    )
                self._push_output("\n".join(lines))
                return
            if command == "/compact":
                if arg.isdigit():
                    message = compact_session(session, keep=int(arg))
                else:
                    message = compact_session(session, needle=arg)
                container.save_session(session)
                self._add_timeline(message)
                self._push_output(f"[green]{message}[/green]")
                return
            if command == "/replan":
                last_run = session.last_task_run
                if last_run is None or last_run.mode != "orchestrated":
                    self._push_output("[dim]No orchestrated run to replan.[/dim]")
                    return
                if last_run.status == "success":
                    self._push_output("[dim]Last orchestration succeeded — nothing to replan.[/dim]")
                    return
                # Find failed subtask index and rerun from there
                from runtime.orchestrator_service import OrchestratorService
                resume_idx = OrchestratorService.find_retry_start_index(last_run)
                if resume_idx is None:
                    resume_idx = 0
                plan = session.last_plan
                if plan is None:
                    self._push_output("[dim]No saved plan to replan from.[/dim]")
                    return
                self._add_timeline(f"Replanning from step {resume_idx + 1}.")
                self._push_output(
                    f"[dim]Replanning from step {resume_idx + 1}/{len(plan.subtasks)}…[/dim]"
                )
                task_run, aggregate_result = await container.orchestrator_service.run_orchestrated_task(
                    session=session,
                    plan=plan,
                    resume_from=resume_idx,
                    prior_subtasks=list(last_run.subtasks),
                )
                container.remember_task_result(session, aggregate_result)
                self._add_timeline(f"Replan done status={task_run.status}.")
                self._append_stream(
                    "",
                    f"  [{'green' if task_run.status == 'success' else 'red'}]"
                    f"{'✓' if task_run.status == 'success' else '✗'}[/]  "
                    f"replan {task_run.status}",
                    *(
                        [_md_to_rich(aggregate_result.answer_text.strip()[:2000])]
                        if aggregate_result.answer_text.strip() else []
                    ),
                )
                return
            if command == "/cd":
                if not arg:
                    self._push_output(f"[dim]Usage: /cd <path>[/dim]")
                    return
                try:
                    new_path = session.file_mgr.set_working_dir(arg)
                    container.save_session(session)
                    self._add_timeline(f"cd → {new_path}")
                    self._refresh_all()
                    self._append_stream(f"  [green]✓[/green] Working dir: [bold]{new_path}[/bold]")
                except Exception as exc:
                    self._append_stream(f"  [red]cd failed: {exc}[/red]")
                return
            if command == "/recover":
                if arg == "discard":
                    container.session_store.clear_checkpoint(session.chat_id)
                    self._push_output("[dim]Checkpoint cleared.[/dim]")
                    return
                checkpoint = container.session_store.load_checkpoint(session)
                if checkpoint is None:
                    self._push_output("[dim]No crash checkpoint found.[/dim]")
                    return
                completed = [s for s in checkpoint.subtasks if s.status in {"success", "reused"}]
                failed = [s for s in checkpoint.subtasks if s.status == "failed"]
                if arg == "confirm":
                    plan = session.last_plan
                    if plan is None:
                        self._push_output("[dim]No saved plan for this checkpoint.[/dim]")
                        return
                    if self._active_task is not None and not self._active_task.done():
                        self._append_stream(
                            "  [dim]⏳ Task running — press [bold]Esc[/bold] to cancel first[/dim]"
                        )
                        return
                    resume_idx = len(completed)
                    self._add_timeline(f"Recovering from checkpoint, step {resume_idx + 1}.")
                    self._push_output(
                        f"[dim]Resuming from step {resume_idx + 1}/{len(plan.subtasks)}…[/dim]"
                    )
                    task_run, aggregate_result = await container.orchestrator_service.run_orchestrated_task(
                        session=session,
                        plan=plan,
                        resume_from=resume_idx,
                        prior_subtasks=list(checkpoint.subtasks),
                    )
                    container.remember_task_result(session, aggregate_result)
                    self._add_timeline(f"Recovery done status={task_run.status}.")
                    self._append_stream(
                        "",
                        f"  [{'green' if task_run.status == 'success' else 'red'}]"
                        f"{'✓' if task_run.status == 'success' else '✗'}[/]  "
                        f"recover {task_run.status}",
                        *(
                            [_md_to_rich(aggregate_result.answer_text.strip()[:2000])]
                            if aggregate_result.answer_text.strip() else []
                        ),
                    )
                    return
                # No arg — show checkpoint info
                self._push_output(
                    "\n".join([
                        f"[bold]Checkpoint found:[/bold]  {checkpoint.run_id}",
                        f"  prompt:    {checkpoint.prompt[:100]}",
                        f"  completed: {len(completed)}/{len(checkpoint.subtasks)} subtasks"
                        + (f"  ({', '.join(s.title for s in completed[:3])})" if completed else ""),
                        *(["  [red]failed:[/red]   " + ", ".join(s.title for s in failed)] if failed else []),
                        "",
                        "Run [bold]/recover confirm[/bold] to resume,",
                        "or  [bold]/recover discard[/bold]  to clear.",
                    ])
                )
                return
            if command == "/cwd":
                cwd = str(session.file_mgr.get_working_dir())
                self._push_output(f"[bold]{cwd}[/bold]")
                return
            if command == "/copy":
                answer = ""
                if session.last_task_result:
                    answer = session.last_task_result.answer_text.strip()
                if answer:
                    msg = _clipboard_copy(answer)
                    self._add_timeline(msg[:80])
                    self._append_stream(f"  [dim]{msg}[/dim]")
                else:
                    self._append_stream("  [dim]Nothing to copy yet.[/dim]")
                return
            if command == "/paste":
                text = _clipboard_paste()
                if text:
                    safe = text[:1000].replace("[", "\\[")
                    self._push_output(f"[dim]Clipboard content:[/dim]\n{safe}")
                else:
                    self._push_output("[dim]Clipboard is empty or unavailable.[/dim]")
                return
            if command == "/history":
                n = 20
                if arg:
                    try:
                        n = int(arg)
                    except ValueError:
                        pass
                hist = self._input_history[-n:]
                if not hist:
                    self._push_output("[dim]No history yet.[/dim]")
                    return
                self._push_output(
                    "[bold]Input history[/bold]\n" +
                    "\n".join(
                        f"  [dim]{i}.[/dim]  {entry.replace('[', chr(92) + '[')}"
                        for i, entry in enumerate(reversed(hist), 1)
                    )
                )
                return
            if command == "/diff":
                cwd = str(session.file_mgr.get_working_dir())
                try:
                    result = _subprocess.run(
                        ["git", "diff", "--stat", "HEAD"],
                        capture_output=True, text=True, cwd=cwd, timeout=5,
                    )
                    output = result.stdout.strip() or result.stderr.strip()
                    if not output:
                        output = "No changes."
                    self._push_output(output.replace("[", "\\["))
                except Exception as exc:
                    self._push_output(f"[red]git diff failed: {exc}[/red]")
                return
            if command == "/commit":
                message = build_commit_message(session, arg)
                ok, output = run_git_commit(cwd=str(session.file_mgr.get_working_dir()), message=message)
                self._add_timeline(f"Commit: {output[:72]}")
                self._push_output(f"[{'green' if ok else 'yellow'}]{output}[/{'green' if ok else 'yellow'}]")
                return
            if command == "/model":
                # Known model catalogs per provider
                _MODEL_CATALOG: dict[str, list[tuple[str, str]]] = {
                    "qwen": [
                        ("qwen3-coder-plus",             "Qwen3 best quality  [default]"),
                        ("qwen3-coder",                  "Qwen3 balanced"),
                        ("qwen3-235b-a22b",              "Qwen3 open-weights 235B MoE"),
                        ("qwen3-32b",                    "Qwen3 open-weights 32B"),
                        ("qwen-coder-plus",              "Qwen2.5 best quality"),
                        ("qwen-coder-turbo",             "Qwen2.5 fast balanced"),
                    ],
                    "codex": [
                        ("gpt-5.4",       "GPT-5.4  [default]"),
                        ("gpt-5.4-mini",  "GPT-5.4 Mini — fast, cheap"),
                        ("gpt-5.2",       "GPT-5.2"),
                        ("gpt-5.1-mini",  "GPT-5.1 Mini — lightweight"),
                    ],
                    "claude": [
                        ("claude-sonnet-4-6",            "Claude Sonnet 4.6  [default]"),
                        ("claude-opus-4-6",              "Claude Opus 4.6 — highest quality"),
                        ("claude-haiku-4-5-20251001",    "Claude Haiku 4.5 — fastest"),
                        ("sonnet",                       "alias → latest sonnet"),
                        ("opus",                         "alias → latest opus"),
                        ("haiku",                        "alias → latest haiku"),
                    ],
                }
                color = self._provider_color()

                # /model — show all providers
                if not arg:
                    lines = ["[bold]Active models[/bold]", ""]
                    for pname in container.provider_paths:
                        cur = session.provider_models.get(pname, "")
                        runtime = session.runtimes.get(pname)
                        actual = runtime.manager.model_name if (runtime and hasattr(runtime, "manager")) else ""
                        display = cur or actual or "[dim]default[/dim]"
                        lines.append(f"  [{color}]{pname:<10}[/{color}]  {display}")
                        catalog = _MODEL_CATALOG.get(pname, [])
                        for mname, mdesc in catalog:
                            marker = "▶" if (cur or actual) == mname else " "
                            lines.append(f"    [dim]{marker} {mname:<36} {mdesc}[/dim]")
                        lines.append("")
                    lines.append("[dim]Usage:  /model <provider> <model>    e.g. /model qwen qwen-coder-plus[/dim]")
                    lines.append("[dim]         /model <provider> default     reset to provider default[/dim]")
                    self._push_output("\n".join(lines))
                    return

                # /model <provider>  OR  /model <provider> <model>
                parts_arg = arg.split(maxsplit=1)
                target_provider = parts_arg[0].lower()
                if target_provider not in container.provider_paths:
                    # Maybe arg is just a model name for current provider?
                    target_provider = session.current_provider
                    new_model = arg.strip()
                else:
                    new_model = parts_arg[1].strip() if len(parts_arg) > 1 else ""

                if not new_model:
                    # Show models for that provider
                    cur = session.provider_models.get(target_provider, "")
                    lines = [f"[bold]{target_provider} models[/bold]", ""]
                    for mname, mdesc in _MODEL_CATALOG.get(target_provider, []):
                        marker = "▶" if cur == mname else " "
                        style = "bold" if cur == mname else "dim"
                        lines.append(f"  [{style}]{marker} {mname:<38} {mdesc}[/{style}]")
                    lines.append("")
                    lines.append(f"[dim]Current: {cur or 'provider default'}[/dim]")
                    lines.append(f"[dim]/model {target_provider} <model>   to switch[/dim]")
                    self._push_output("\n".join(lines))
                    return

                # Actually set the model
                if new_model.lower() == "default":
                    new_model = ""

                session.provider_models[target_provider] = new_model
                container.save_session(session)

                # Rebuild the runtime for this provider so the next run uses the new model
                old_runtime = session.runtimes.pop(target_provider, None)
                if old_runtime and old_runtime.manager.is_running:
                    import asyncio as _asyncio
                    _asyncio.create_task(old_runtime.manager.stop())

                label = new_model if new_model else "default"
                self._add_timeline(f"Model {target_provider} → {label}")
                self._push_output(
                    f"[{color}]✓[/{color}]  [{color}]{target_provider}[/{color}] model set to [bold]{label}[/bold]\n"
                    f"[dim]  The new model will be used on the next prompt.[/dim]"
                )
                # Refresh welcome screen so the model shows there too
                if self.current_mode == "idle":
                    self._push_output(self._welcome_text())
                return
            if command == "/retry":
                last = session.last_task_result
                if last and last.prompt:
                    await self._run_prompt(last.prompt)
                else:
                    self._push_output("[dim]No previous prompt to retry.[/dim]")
                return
            if command == "/expand":
                last = session.last_task_result
                if last and last.answer_text.strip():
                    color = self._provider_color()
                    self._push_output(
                        f"[{color}]◆ {last.provider}[/]  [dim]full answer[/dim]\n\n"
                        + _md_to_rich(last.answer_text.strip())
                    )
                else:
                    self._push_output("[dim]No answer to expand.[/dim]")
                return
            if command == "/help":
                self._add_timeline("Opened help.")
                self._push_output("\n".join(grouped_help_lines() + [
                    "",
                    "[bold]Keys[/bold]",
                    "  /          open command dropdown (↑↓ navigate, Tab/Enter complete)",
                    "  Ctrl+F     search stream (Esc to close)",
                    "  Ctrl+R     reverse history search",
                    "  Ctrl+Y     copy last answer to clipboard",
                    "  Ctrl+V     paste from clipboard",
                    "  Shift+Enter  start multi-line input",
                    "  ↑ / ↓      input history navigation",
                    "  A          toggle autoscroll (when input not focused)",
                    "  Ctrl+C     cancel active task",
                ]))
                return
            if command == "/provider":
                if not arg:
                    self._push_output(f"provider: {session.current_provider}")
                    return
                from providers import is_supported_provider, normalize_provider_name

                provider = normalize_provider_name(arg)
                if not is_supported_provider(provider):
                    self._push_output(f"Unsupported provider: {arg}")
                    return
                session.current_provider = provider
                container.save_session(session)
                self.current_provider = provider
                self._apply_provider_theme()
                self._add_timeline(f"Provider set to {provider}.")
                self._refresh_all()
                # Show welcome screen with new provider logo so the brand change is visible
                self._push_output(self._welcome_text())
                return
            if command == "/providers":
                self._push_output(
                    "\n".join(
                        f"{name} · {path}"
                        for name, path in container.provider_paths.items()
                    )
                )
                return
            if command == "/status":
                self._add_timeline("Viewed status.")
                self._push_output(
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
                provider_lines: list[str] = ["[bold]Provider status[/bold]", ""]
                for provider_name, cli_path in container.provider_paths.items():
                    label, col = self._probe_provider_status(provider_name, cli_path)
                    runtime = session.runtimes.get(provider_name)
                    line = f"  [{col}]●[/{col}]  [bold]{provider_name}[/bold]  [{col}]{label}[/{col}]"
                    if runtime and runtime.health:
                        h = runtime.health
                        line += f"  [dim]ctx:{h.context_status}[/dim]"
                        if h.consecutive_failures:
                            line += f"  [dim]{h.consecutive_failures} consecutive fail(s)[/dim]"
                        if h.last_failure and h.last_failure.message:
                            line += f"\n       [dim]{h.last_failure.message[:80]}[/dim]"
                    provider_lines.append(line)
                self._push_output("\n".join(provider_lines))
                return
            if command == "/usage":
                self._push_output("\n".join(render_usage_lines(session, container.provider_paths)))
                return
            if command == "/metrics":
                self._push_output(container.metrics.render_prometheus().replace("[", "\\["))
                return
            if command == "/todos":
                self._push_output("\n".join(render_todos_lines(session)))
                return
            if command == "/runs":
                self._add_timeline("Viewed runs.")
                runs = container.recent_runs(session, limit=10)
                if not runs:
                    self._push_output("No runs yet.")
                    return
                self._push_output(
                    "\n".join(
                        f"{index}. {run.status_emoji} {run.mode} [{run.provider_summary or 'mixed'}]"
                        for index, run in enumerate(runs, start=1)
                    )
                )
                return
            if command == "/show":
                if not arg:
                    self._push_output("Usage: /show <index>")
                    return
                try:
                    index = int(arg)
                except ValueError:
                    self._push_output("Run index must be a number.")
                    return
                run = container.run_by_index(session, index)
                if run is None:
                    self._push_output(f"Run {index} not found.")
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
                self._push_output("\n".join(details))
                return
            if command == "/artifacts":
                artifacts = container.latest_artifact_files(session)
                if not artifacts:
                    self._push_output("No artifacts found.")
                    return
                self._push_output("\n".join(str(item) for item in artifacts))
                return
            if command == "/review":
                ok, provider_or_message, output = await run_review_pass(container, session, arg)
                if ok:
                    self._add_timeline(f"Reviewed via {provider_or_message}.")
                    self._push_output(
                        f"[bold]Review · {provider_or_message}[/bold]\n\n"
                        + _md_to_rich((output or "Empty review.").strip()[:6000])
                    )
                else:
                    self._push_output(f"[red]{(output or provider_or_message)}[/red]")
                return
            if command == "/plan":
                if not arg:
                    self._push_output("Usage: /plan <task>")
                    return
                color = self._provider_color()
                planning_provider = container.pick_planning_provider(session)
                self._append_stream(
                    "",
                    "  [dim]" + "─  " * 28 + "[/dim]",
                    f"  [dim]>[/dim] [white]{arg[:120]}[/white]",
                    f"[{color}]◆[/] [bold]Planning…[/bold]  [dim][{planning_provider}][/dim]",
                    "",
                )
                self._status_state = {"action": "Planning…", "tokens": 0, "start": _time.monotonic(), "input_tokens": 0, "output_tokens": 0}
                self._show_status_line()

                def _plan_stream_cb(line: str):
                    self._update_status_event(line)

                plan = await self._build_plan_ai(session, arg, stream_event_callback=_plan_stream_cb)
                self._hide_status_line()
                session.last_plan = plan
                container.save_session(session)
                self._add_timeline(f"Planned: {arg[:48]}")
                ai_tag = "  [dim](AI plan)[/dim]" if plan.ai_rationale else "  [dim](rule-based)[/dim]"
                eta = container.orchestrator_service.estimate_plan_eta(plan, session)
                cached_tag = "  [dim][cached][/dim]" if "[cached]" in plan.ai_rationale else ""
                color = self._provider_color()
                prov_colors = {"qwen": "#b07cff", "codex": "#6aa7ff", "claude": "#ff9e57"}
                panel_lines = self._plan_panel_lines(
                    strategy=plan.strategy,
                    complexity=f"{plan.complexity}{ai_tag}{cached_tag}",
                    eta=eta,
                    rationale=plan.ai_rationale,
                )
                table_lines = self._subtask_table_lines(plan.subtasks, prov_colors)
                all_lines = [
                    *panel_lines,
                    *table_lines,
                    "",
                    f"  Run this plan?  [{color}]Y[/{color}][dim]/n[/dim]  ·  or use [bold]/run-plan[/bold]  [dim]/edit-plan[/dim]",
                ]
                self._push_output("\n".join(all_lines))
                self._awaiting_plan_confirm = True
                self.query_one("#input", Input).placeholder = "y to run · n to cancel · /edit-plan to adjust"
                return
            if command == "/run-plan":
                plan = session.last_plan
                if plan is None:
                    self._push_output("[dim]No saved plan. Use /plan <task> first.[/dim]")
                    return
                if self._active_task is not None and not self._active_task.done():
                    self._append_stream(
                        "  [dim]⏳ Task running — press [bold]Esc[/bold] to cancel first[/dim]"
                    )
                    return
                self._active_task = asyncio.create_task(
                    self._run_orchestration(plan.prompt, prebuilt_plan=plan)
                )
                return
            if command == "/orchestrate":
                if not arg:
                    self._push_output("Usage: /orchestrate <task>")
                    return
                if self._active_task is not None and not self._active_task.done():
                    self._append_stream(
                        "  [dim]⏳ Task running — press [bold]Esc[/bold] to cancel first[/dim]"
                    )
                    return
                self._active_task = asyncio.create_task(self._run_orchestration(arg))
                return
            if command == "/remote-control":
                from cli.remote_control import RemoteControlManager

                manager = RemoteControlManager()
                action = arg or "start"
                if action == "start":
                    try:
                        status = manager.start()
                    except RuntimeError as exc:
                        self._push_output(str(exc))
                        return
                    self.remote_state = "running" if status.is_running else "stopped"
                    self._add_timeline("Remote control started.")
                    self._refresh_all()
                    self._push_output(f"Remote control started. log: {status.log_path}")
                    return
                if action == "status":
                    status = manager.load_status()
                    self.remote_state = "running" if status.is_running else "stopped"
                    self._add_timeline("Checked remote-control status.")
                    self._refresh_all()
                    self._push_output(
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
                    self._push_output("Remote control stopped.")
                    return
                if action == "logs":
                    logs = manager.tail_logs()
                    self._push_output(logs or "No remote-control logs yet.")
                    return
                self._push_output("Usage: /remote-control [status|stop|logs]")
                return

            self._push_output(f"Unknown command: {raw}")

        async def _run_prompt(self, prompt: str, provider_override: str | None = None):
            from providers import normalize_provider_name
            try:
              await self._run_prompt_inner(prompt, provider_override)
            except asyncio.CancelledError:
                if self._active_runtime is not None:
                    asyncio.create_task(self._active_runtime.manager.stop())
                    self._active_runtime = None
                self._hide_status_line()
                self.current_mode = "idle"
                self._active_task = None
            except Exception:
                self._active_task = None
                raise
            else:
                self._active_task = None

        async def _run_prompt_inner(self, prompt: str, provider_override: str | None = None):
            from providers import normalize_provider_name

            session = container.get_session(chat_id)
            provider_name = normalize_provider_name(provider_override or session.current_provider)
            runtime = await container.ensure_runtime_started(session, provider_name)
            self.current_mode = "single"
            self._active_runtime = runtime
            self._set_orchestration_steps([])
            self._add_timeline(f"Started: {prompt[:48]}")
            self._refresh_all()
            self._task_start_time = _time.monotonic()

            cwd = str(session.file_mgr.get_working_dir())
            expanded_prompt = _expand_file_mentions(prompt, cwd)
            color = {
                "qwen": "#b07cff", "codex": "#6aa7ff", "claude": "#ff9e57",
            }.get(provider_name, "#6aa7ff")
            override_tag = f"  [dim](via {provider_override})[/dim]" if provider_override else ""
            sep = "  [dim]" + "─  " * 28 + "[/dim]"
            self._dim_stream_history()
            self._append_stream(
                "",
                sep,
                f"  [dim]>[/dim] [white]{prompt[:120]}[/white]{override_tag}",
                f"  [dim]{provider_name}  ·  {cwd}[/dim]",
                "",
            )

            self._status_state = {"action": "Starting…", "tokens": 0, "start": 0.0, "input_tokens": 0, "output_tokens": 0}
            self._last_stream_was_text = False
            self._streamed_text_start = None
            self._op_type = ""
            self._op_files = []
            self._op_line_idx = -1
            self._text_buffer = ""
            self._text_in_code = False
            self._text_has_partial = False
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

            # notify-send for long tasks
            elapsed = _time.monotonic() - self._task_start_time
            if elapsed >= 60:
                try:
                    _subprocess.run(
                        ["notify-send", "-t", "5000", "Bridge CLI",
                         f"{provider_name} finished in {int(elapsed)}s"],
                        timeout=3, capture_output=True,
                    )
                except Exception:
                    pass

            # File diffs — use provider's accent color for added lines
            diff_lines: list[str] = []
            for f in result.new_files:
                diff_lines.extend(_file_diff_text(f, is_new=True, add_color=color))
            for f in result.changed_files:
                diff_lines.extend(_file_diff_text(f, is_new=False, add_color=color))

            # Final result — if we streamed raw text, replace it with formatted markdown
            status_icon = f"[{color}]✓[/]" if result.exit_code == 0 else "[red]✗[/red]"
            duration = getattr(result, "duration_text", "")
            answer = result.answer_text.strip()

            if self._streamed_text_start is not None:
                # Drop streamed lines; replace with the final full render.
                # This corrects any multi-line constructs (tables, code blocks)
                # that couldn't be resolved during per-chunk streaming.
                self._stream_lines = self._stream_lines[:self._streamed_text_start]
                self._last_stream_was_text = False
                self._text_buffer = ""
                self._text_in_code = False
                self._text_rendered_count = 0

            final_parts: list[str] = ["", *diff_lines]
            if answer:
                final_parts += ["", _md_to_rich(answer[:3000])]
            elif result.exit_code != 0 and result.error_text:
                final_parts += ["", f"[red]{result.error_text[:500]}[/red]"]
            files_changed = len(result.new_files) + len(result.changed_files)
            files_part = f"  ·  [dim]{files_changed} file{'s' if files_changed != 1 else ''} changed[/dim]" if files_changed else ""
            done_label = "Done" if result.exit_code == 0 else "Failed"
            final_parts += [
                "",
                f"  {status_icon} {done_label}  [dim]·  {provider_name}  ·  {duration}[/dim]{files_part}",
            ]
            self._append_stream(*final_parts)

        async def _run_orchestration(self, prompt: str, prebuilt_plan=None):
            session = container.get_session(chat_id)
            self.current_mode = "orchestrated"
            self._add_timeline("Started orchestration.")
            self._refresh_all()
            self._task_start_time = _time.monotonic()
            cwd_for_expand = str(session.file_mgr.get_working_dir())
            expanded_prompt = _expand_file_mentions(prompt, cwd_for_expand)

            # Use prebuilt plan (from /run-plan) or build one fresh
            self._status_state = {"action": "Planning…", "tokens": 0, "start": 0.0, "input_tokens": 0, "output_tokens": 0}
            self._show_status_line()
            if prebuilt_plan is not None:
                plan = prebuilt_plan
            else:
                def _orch_plan_stream_cb(line: str):
                    self._update_status_event(line)

                plan = await self._build_plan_ai(session, expanded_prompt, stream_event_callback=_orch_plan_stream_cb)
            session.last_plan = plan
            container.save_session(session)

            cwd = str(session.file_mgr.get_working_dir())
            color = self._provider_color()
            ai_tag = "  [dim](AI)[/dim]" if plan.ai_rationale else "  [dim](rule-based)[/dim]"
            has_parallel = any(s.parallel_group > 0 for s in plan.subtasks)

            # Show synthesis/review only if they'll actually run
            will_synthesize = len(plan.subtasks) >= 2
            will_review = plan.complexity == "complex"
            step_labels = [
                f"[pending] {i}. {item.title} [{item.suggested_provider}]"
                + (f" ∥{item.parallel_group}" if has_parallel else "")
                for i, item in enumerate(plan.subtasks, start=1)
            ]
            if will_synthesize:
                step_labels.append("[pending] synthesis")
            if will_review:
                step_labels.append("[pending] review")
            self._set_orchestration_steps(step_labels)

            self._dim_stream_history()
            self._append_stream(
                "",
                "  [dim]" + "─  " * 28 + "[/dim]",
                f"  [dim]>[/dim] [white]{prompt[:120]}[/white]{ai_tag}",
                f"  [dim]orchestrate  ·  {cwd}[/dim]",
                "",
                f"  strategy: {plan.strategy}",
                *(["", f"  [dim]{plan.ai_rationale}[/dim]"] if plan.ai_rationale else []),
                *[
                    "  [dim]{i}. {t} [{p}]{pg}[/dim]".format(
                        i=i, t=item.title, p=item.suggested_provider,
                        pg=f" ∥group={item.parallel_group}" if has_parallel else "",
                    )
                    for i, item in enumerate(plan.subtasks, start=1)
                ],
            )
            current_step = {"index": -1}
            step_start = [_time.monotonic()]
            synthesis_idx = len(plan.subtasks)
            review_idx = synthesis_idx + (1 if will_synthesize else 0)

            self._status_state.update({"action": "Executing…", "tokens": 0})
            self._last_stream_was_text = False

            _prov_colors = {"qwen": "#b07cff", "codex": "#6aa7ff", "claude": "#ff9e57"}

            async def status_callback(text: str):
                if not text:
                    return
                clean = _strip_html(text).strip()
                lowered = clean.lower()
                if "шаг " in lowered and "агент:" in lowered:
                    # Parse 1-based step number directly from message to avoid
                    # incrementing on every periodic update_status_loop tick
                    m_step = _re.search(r'шаг (\d+)/', lowered)
                    if m_step:
                        next_index = min(int(m_step.group(1)) - 1, len(plan.subtasks) - 1)
                    else:
                        next_index = min(current_step["index"] + 1, max(0, len(plan.subtasks) - 1))
                    # Parse actual provider name from message for per-agent colour
                    m_prov = _re.search(r'агент:\s*(\w+)', lowered)
                    prov_name = m_prov.group(1) if m_prov else ""
                    step_color = _prov_colors.get(prov_name, self._provider_color())
                    if next_index != current_step["index"]:
                        # Genuinely new step — emit header once and reset timer
                        if current_step["index"] >= 0:
                            self._mark_orchestration_step(current_step["index"], "done")
                        current_step["index"] = next_index
                        self._mark_orchestration_step(next_index, "running")
                        subtask = plan.subtasks[next_index]
                        step_start[0] = _time.monotonic()
                        step_cwd = str(session.file_mgr.get_working_dir())
                        self._status_state.update({"action": f"Step {next_index+1}/{len(plan.subtasks)}…", "tokens": 0, "start": step_start[0]})
                        self._append_stream(
                            "",
                            "  [dim]" + "─  " * 14 + "[/dim]",
                            f"[{step_color}]▶[/] [bold]Step {next_index+1}/{len(plan.subtasks)}[/bold]  {subtask.title}  [dim][{prov_name or subtask.suggested_provider}][/dim]",
                            f"  [dim]{step_cwd}[/dim]",
                        )
                    # Periodic updates for the same step: just refresh status label
                    else:
                        self._status_state.update({"action": f"Step {next_index+1}/{len(plan.subtasks)}…"})
                elif "собирает итог" in lowered:
                    if not current_step.get("synthesis_shown"):
                        current_step["synthesis_shown"] = True
                        if 0 <= current_step["index"] < len(plan.subtasks):
                            self._mark_orchestration_step(current_step["index"], "done")
                        if will_synthesize:
                            self._mark_orchestration_step(synthesis_idx, "running")
                        self._status_state.update({"action": "Synthesizing…", "tokens": 0, "start": _time.monotonic()})
                        self._append_stream("", "  [dim]" + "─  " * 14 + "[/dim]", f"[{color}]▶[/] [bold]Synthesis[/bold]")
                elif "выполняет review" in lowered:
                    if not current_step.get("review_shown"):
                        current_step["review_shown"] = True
                        if will_synthesize:
                            self._mark_orchestration_step(synthesis_idx, "done")
                        if will_review:
                            self._mark_orchestration_step(review_idx, "running")
                        self._status_state.update({"action": "Reviewing…", "tokens": 0, "start": _time.monotonic()})
                        self._append_stream("", "  [dim]" + "─  " * 14 + "[/dim]", f"[{color}]▶[/] [bold]Review[/bold]")
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
            synthesis_idx = len(plan.subtasks)
            review_idx = synthesis_idx + (1 if will_synthesize else 0)
            if will_synthesize:
                self._mark_orchestration_step(
                    synthesis_idx, "done" if task_run.synthesis_answer else "skipped"
                )
            if will_review:
                self._mark_orchestration_step(
                    review_idx, "done" if task_run.review_answer else "skipped"
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

            # notify-send for long orchestrations
            elapsed_orch = _time.monotonic() - self._task_start_time
            if elapsed_orch >= 60:
                try:
                    _subprocess.run(
                        ["notify-send", "-t", "5000", "Bridge CLI",
                         f"Orchestration {task_run.status} in {int(elapsed_orch)}s"],
                        timeout=3, capture_output=True,
                    )
                except Exception:
                    pass

    return BridgeTextualApp()


def run_textual_shell(container, chat_id: int = 0):
    create_textual_app(container, chat_id=chat_id).run()
