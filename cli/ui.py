import os
import re as _re
import subprocess as _subprocess
import time as _time
from pathlib import Path

from cli.thinking import extract_thinking_chunk, render_thinking_text

from core.providers import provider_default_model


# ── inline markdown → Rich markup ─────────────────────────────────────────────

def _render_md_inline(text: str) -> str:
    """
    Convert common markdown markers to Rich markup tags for inline streaming.

    Handles single lines / short chunks; does NOT attempt to parse fenced
    code blocks (those are multi-line and handled by the thinking/diff renderers).
    """
    # Escape Rich markup characters that might appear in code/paths
    # We process in order: code first (to protect its contents), then bold/italic.

    # Inline code: `code`  →  dim bold
    text = _re.sub(r"`([^`\n]{1,120})`", r"[bold dim]\1[/bold dim]", text)

    # Bold: **text** or __text__
    text = _re.sub(r"\*\*(.{1,200}?)\*\*", r"[bold]\1[/bold]", text)
    text = _re.sub(r"__(.{1,200}?)__", r"[bold]\1[/bold]", text)

    # Italic: *text* (single star, not at word boundaries to avoid false positives)
    text = _re.sub(r"(?<!\*)\*([^*\n]{1,100})\*(?!\*)", r"[italic]\1[/italic]", text)

    # Markdown headings → bold (only at line start)
    text = _re.sub(r"^#{1,3} (.+)$", r"[bold]\1[/bold]", text, flags=_re.MULTILINE)

    # List bullets — keep "- " but make them slightly accented
    text = _re.sub(r"^([ \t]*)[*\-] ", r"\1• ", text, flags=_re.MULTILINE)

    return text

try:
    from rich.align import Align
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.padding import Padding
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.columns import Columns
    from rich.text import Text
except ImportError:  # pragma: no cover - fallback path is exercised implicitly
    Align = None
    Console = None
    Group = None
    Layout = None
    Live = None
    Padding = None
    Panel = None
    Rule = None
    Syntax = None
    Table = None
    Columns = None
    Text = None


# ── helpers ──────────────────────────────────────────────────────────────────

_LANG_MAP = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "jsx": "jsx", "tsx": "tsx", "rs": "rust", "go": "go",
    "c": "c", "cpp": "cpp", "h": "c", "hpp": "cpp",
    "sh": "bash", "bash": "bash", "zsh": "bash", "fish": "fish",
    "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml",
    "md": "markdown", "html": "html", "css": "css",
    "sql": "sql", "nim": "nim", "lua": "lua",
}

def _detect_lang(path: Path) -> str:
    return _LANG_MAP.get(path.suffix.lstrip(".").lower(), "text")


def _git_diff(path: Path) -> str | None:
    """Return git diff for path, or None if not available / no diff."""
    for args in (
        ["git", "diff", "HEAD", "--", str(path)],
        ["git", "diff", "--", str(path)],
        ["git", "diff", "--cached", "--", str(path)],
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


class CliUi:
    def __init__(self):
        self.console = Console() if Console else None

    def _provider_style(self, provider: str) -> str:
        mapping = {
            "qwen": "bright_magenta",
            "codex": "bright_cyan",
            "claude": "orange3",
            "openrouter": "green3",
        }
        return mapping.get(provider, "cyan")

    def _provider_label(self, provider: str) -> str:
        return provider.upper() if provider else "AUTO"

    def _provider_tagline(self, provider: str) -> str:
        mapping = {
            "qwen": "python · scripting · data",
            "codex": "systems · backend · refactor",
            "claude": "ui · ux · writing",
            "openrouter": "api · planning · review",
        }
        return mapping.get(provider, "general purpose")

    def _ansi_provider_color(self, provider: str) -> str:
        mapping = {
            "qwen": "\033[95m",
            "codex": "\033[96m",
            "claude": "\033[33m",
            "openrouter": "\033[92m",
        }
        return mapping.get(provider, "\033[36m")

    def _ansi_dim(self) -> str:
        return "\033[2m"

    def _ansi_bold(self) -> str:
        return "\033[1m"

    def _ansi_reset(self) -> str:
        return "\033[0m"

    def _terminal_width(self, default: int = 110) -> int:
        try:
            return os.get_terminal_size().columns
        except OSError:
            return default

    def _box(self, title: str, lines: list[str], provider: str = "") -> str:
        accent = self._ansi_provider_color(provider) if provider else "\033[36m"
        reset = self._ansi_reset()
        width = min(max(self._terminal_width(), 80), 140)
        inner = width - 4
        safe_title = f" {title} " if title else ""
        top_fill = "─" * max(0, inner - len(safe_title))
        top = f"{accent}┌{safe_title}{top_fill}┐{reset}"
        body = []
        for line in lines:
            trimmed = line[:inner]
            body.append(f"{accent}│{reset} {trimmed.ljust(inner)} {accent}│{reset}")
        bottom = f"{accent}└{'─' * (width - 2)}┘{reset}"
        return "\n".join([top, *body, bottom])

    def clear(self):
        if self.console:
            self.console.clear()
        else:
            print("\033c", end="")

    def print_line(self, text: str = ""):
        if self.console:
            self.console.print(text)
        else:
            print(text)

    def print_kv(self, key: str, value: str):
        if self.console:
            self.console.print(f"[bold]{key}[/bold]: {value}")
        else:
            print(f"{self._ansi_bold()}{key}{self._ansi_reset()}: {value}")

    def print_status(self, text: str):
        if self.console and Panel:
            self.console.print(Panel(text, title="Status", border_style="cyan"))
        else:
            print(text)
            print()

    def print_shell_footer(self):
        footer = "Use /help for commands · /new to clear the workspace · /remote-control to start Telegram access"
        if self.console:
            self.console.print(f"[bright_black]{footer}[/bright_black]")
        else:
            print(footer)

    def supports_live(self) -> bool:
        return bool(self.console and Live and Panel)

    def render_home_screen(self, session, recent_runs, remote_status):
        self.print_home(session, recent_runs, remote_status)

    def print_shell_chrome(self, provider: str, cwd: str, remote_status):
        provider_style = self._provider_style(provider)
        if self.console and Rule:
            self.console.print(Rule(f"[{provider_style}]>_ {self._provider_label(provider)} Shell[/]", style=provider_style))
            self.console.print(
                f"[bright_black]{self._provider_tagline(provider)} · {cwd} · remote: {'running' if remote_status.is_running else 'stopped'}[/bright_black]"
            )
            return
        title = f">_ {self._provider_label(provider)} Shell"
        body = "\n".join(
            [
                f"cwd: {cwd}",
                f"remote: {'running' if remote_status.is_running else 'stopped'}",
            ]
        )
        self.print_block(title, body, border_style=provider_style)

    def print_input_bar(self, provider: str, remote_status):
        # Called before each prompt — keep silent; the prompt itself carries enough context.
        pass

    def print_block(self, title: str, text: str, border_style: str = "cyan"):
        if self.console and Panel:
            self.console.print(Panel(text, title=title, border_style=border_style))
        else:
            provider = ""
            lowered = border_style.lower()
            if "magenta" in lowered:
                provider = "qwen"
            elif "yellow" in lowered or "orange" in lowered:
                provider = "claude"
            elif "cyan" in lowered or "blue" in lowered:
                provider = "codex"
            print(self._box(title, text.splitlines() or [""], provider=provider))
            print()

    def _banner_text(self):
        lines = [
            "██████  ██████  ██ ██████   ██████  ███████",
            "██   ██ ██   ██ ██ ██   ██ ██       ██",
            "██████  ██████  ██ ██   ██ ██   ███ █████",
            "██   ██ ██   ██ ██ ██   ██ ██    ██ ██",
            "██████  ██   ██ ██ ██████   ██████  ███████",
        ]
        if self.console and Text:
            text = Text()
            styles = ["cyan", "bright_cyan", "bright_blue", "magenta", "bright_magenta"]
            for index, line in enumerate(lines):
                text.append(line, style=styles[index % len(styles)] + " bold")
                if index < len(lines) - 1:
                    text.append("\n")
            return text
        qwen = self._ansi_provider_color("qwen")
        reset = self._ansi_reset()
        return "\n".join(f"{qwen}{line}{reset}" for line in lines)

    def _session_card_text(self, session, remote_status) -> str:
        working_dir = str(session.file_mgr.get_working_dir())
        last_run = session.last_task_run.run_id if session.last_task_run else "No runs yet"
        return "\n".join(
            [
                f"{self._provider_tagline(session.current_provider)}",
                "",
                f"{self._provider_label(session.current_provider)} provider",
                f"remote: {'running' if remote_status.is_running else 'stopped'}",
                f"runs: {len(session.run_history)}",
                f"last: {last_run}",
                working_dir,
            ]
        )

    def _tips_text(self, recent_runs) -> str:
        recent_text = "No recent activity"
        if recent_runs:
            recent_text = "\n".join(
                f"{index}. {run.status_emoji} {run.mode} [{run.provider_summary or 'mixed'}] · {run.duration_text}"
                for index, run in enumerate(recent_runs[:5], start=1)
            )
        return "\n".join(
            [
                "Quick start",
                "/help for commands",
                "/new to reset the shell view",
                "/remote-control to start Telegram access",
                "/provider codex to switch provider",
                "/orchestrate <task> for multi-agent mode",
                "",
                "Recent activity",
                recent_text,
            ]
        )

    def print_home(self, session, recent_runs, remote_status):
        """
        Minimal welcome screen — provider, cwd, recent runs, hint line.
        No ASCII banner; keeps visual weight low so the first prompt
        feels immediate.
        """
        from importlib.metadata import version as _pkg_version
        try:
            forge_version = _pkg_version("forge-ai")
        except Exception:
            forge_version = "dev"

        provider = session.current_provider
        style = self._provider_style(provider)
        cwd = str(session.file_mgr.get_working_dir())
        model = (session.provider_models.get(provider) or "").strip() or ""

        if self.console:
            self.console.print()
            # ── title line ─────────────────────────────────────────────────
            version_part = f"[bright_black]v{forge_version}[/bright_black]  "
            model_part = f"[bright_black]{model}[/bright_black]  " if model else ""
            remote_part = "[yellow]remote on[/yellow]  " if remote_status.is_running else ""
            self.console.print(
                f"  {version_part}[{style}]◆ {provider}[/]  "
                f"{model_part}[bright_black]{cwd}[/bright_black]  "
                f"{remote_part}"
            )
            # ── recent runs ────────────────────────────────────────────────
            if recent_runs:
                self.console.print()
                for run in recent_runs[:5]:
                    prompt_preview = " ".join((run.prompt or "").split())[:60]
                    if len(run.prompt or "") > 60:
                        prompt_preview += "…"
                    self.console.print(
                        f"  [bright_black]{run.status_emoji} {run.duration_text:<6}"
                        f"  {prompt_preview}[/bright_black]"
                    )
            # ── hint line ──────────────────────────────────────────────────
            self.console.print()
            self.console.print(
                "  [bright_black]"
                "↑ history  Tab commands  Ctrl+C cancel  "
                "/help · /plan · /orchestrate · /provider"
                "[/bright_black]"
            )
            self.console.print()
            return

        # ── plain fallback ─────────────────────────────────────────────────
        accent = self._ansi_provider_color(provider)
        dim = self._ansi_dim()
        reset = self._ansi_reset()
        print()
        model_suffix = f"  {model}" if model else ""
        print(f"  {dim}v{forge_version}{reset}  {accent}◆ {provider}{reset}{dim}{model_suffix}  {cwd}{reset}")
        if recent_runs:
            print()
            for run in recent_runs[:5]:
                preview = " ".join((run.prompt or "").split())[:60]
                print(f"  {dim}{run.status_emoji} {run.duration_text:<6}  {preview}{reset}")
        print()
        print(f"  {dim}↑ history  Tab commands  Ctrl+C cancel  /help · /plan · /orchestrate{reset}")
        print()

    def build_prompt(self, provider: str, remote_status, queued: int = 0) -> str:
        remote_icon = "⬡" if remote_status.is_running else ""
        queue = f" +{queued}" if queued else ""
        provider_lower = provider.lower()
        if self.console:
            style = self._provider_style(provider)
            remote_part = f" [{style}]{remote_icon}[/]" if remote_icon else ""
            queue_part = f" [bright_black]{queue}[/]" if queue else ""
            return f"[{style}]◆[/] [bold]{provider_lower}[/bold]{remote_part}{queue_part} [bright_black]›[/bright_black] "
        accent = self._ansi_provider_color(provider)
        dim = self._ansi_dim()
        bold = self._ansi_bold()
        reset = self._ansi_reset()
        remote_part = f" {remote_icon}" if remote_icon else ""
        return f"{accent}◆{reset} {bold}{provider_lower}{reset}{dim}{remote_part}{queue}{reset} {dim}›{reset} "

    def print_shell_help(self, help_lines: list[str] | None = None):
        body = "\n".join(help_lines) if help_lines else "\n".join(
            [
                "/help",
                "/home",
                "/new",
                "/status",
                "/limits",
                "/providers",
                "/provider <qwen|codex|claude>",
                "/plan <task>",
                "/orchestrate <task>",
                "/runs",
                "/show <index>",
                "/artifacts",
                "/remote-control",
                "/remote-control status",
                "/remote-control stop",
                "/remote-control logs",
                "/quit",
                "",
                "Any text without a slash runs a single-agent task through the current provider.",
            ]
        )
        self.print_block(
            "Shell Commands",
            body,
            border_style="yellow",
        )

    def print_remote_status(self, status, message: str = ""):
        lines = []
        if message:
            lines.append(message)
            lines.append("")
        lines.append(f"running: {'yes' if status.is_running else 'no'}")
        if status.pid:
            lines.append(f"pid: {status.pid}")
        if status.started_at:
            lines.append(f"started_at: {status.started_at}")
        if status.log_path:
            lines.append(f"log_path: {status.log_path}")
        if status.state_file:
            lines.append(f"state_file: {status.state_file}")
        self.print_block("Remote Control", "\n".join(lines), border_style="magenta")

    def print_session_status(self, session, remote_status):
        current_model = session.provider_models.get(session.current_provider, "").strip() or provider_default_model(session.current_provider) or "default"
        lines = [
            f"provider: {session.current_provider}",
            f"model: {current_model}",
            f"active_provider: {session.active_provider or '-'}",
            f"working_dir: {session.file_mgr.get_working_dir()}",
            f"queued_tasks: {len(session.pending_tasks)}",
            f"history_entries: {len(session.history)}",
            f"run_entries: {len(session.run_history)}",
            f"remote_control: {'running' if remote_status.is_running else 'stopped'}",
        ]
        if session.last_task_run:
            lines.append(f"last_run: {session.last_task_run.status_emoji} {session.last_task_run.run_id}")
        self.print_block("Session Status", "\n".join(lines), border_style="green")

    def print_provider_limits(self, provider_lines: list[str]):
        text = "\n\n".join(provider_lines) if provider_lines else "No provider state collected yet."
        self.print_block("Provider Limits", text, border_style="yellow")

    def print_notice(self, message: str, provider: str = "", kind: str = "info"):
        if self.console:
            style_map = {
                "info": self._provider_style(provider or "codex"),
                "success": "green",
                "warning": "yellow",
                "error": "red",
            }
            style = style_map.get(kind, self._provider_style(provider or "codex"))
            self.console.print(f"[{style}]•[/] {message}")
            return
        bullet = {
            "info": "*",
            "success": "+",
            "warning": "!",
            "error": "x",
        }.get(kind, "*")
        print(f"{bullet} {message}")

    def print_task_workspace(
        self,
        title: str,
        provider: str,
        mode: str,
        cwd: str,
        prompt: str = "",
        remote_running: bool = False,
    ):
        provider_style = self._provider_style(provider)
        prompt_preview = " ".join(prompt.split())
        if len(prompt_preview) > 220:
            prompt_preview = prompt_preview[:219] + "…"
        if self.console and Panel and Group:
            header = Group(
                Text(f"{self._provider_label(provider)} · {mode}", style=f"{provider_style} bold"),
                Text(cwd, style="bright_black"),
                Text(f"remote: {'running' if remote_running else 'stopped'}", style="bright_black"),
            )
            body_items = [header]
            if prompt_preview:
                body_items.extend(
                    [
                        Text(""),
                        Text("Prompt", style=f"{provider_style} bold"),
                        Text(prompt_preview),
                    ]
                )
            self.console.print(Panel(Group(*body_items), title=title, border_style=provider_style, padding=(1, 2)))
            return
        lines = [
            f"provider: {self._provider_label(provider)}",
            f"mode: {mode}",
            f"cwd: {cwd}",
            f"remote: {'running' if remote_running else 'stopped'}",
        ]
        if prompt_preview:
            lines.append("")
            lines.append("prompt:")
            lines.append(prompt_preview)
        self.print_block(title, "\n".join(lines), border_style=provider_style)

    def print_stream_snapshot(self, title: str, lines: list[str], provider: str = ""):
        if self.console and Panel:
            body = "\n".join(lines[-8:]) if lines else "Waiting for first event..."
            self.console.print(Panel(body, title=title, border_style=self._provider_style(provider or "qwen"), padding=(1, 2)))
            return
        body = "\n".join(lines[-8:]) if lines else "Waiting for first event..."
        self.print_block(title, body, border_style=self._provider_style(provider or "qwen"))

    def print_task_result_summary(self, task_result):
        border_style = self._provider_style(task_result.provider)
        if self.console and Panel:
            lines = [
                Text(f"{self._provider_label(task_result.provider)}", style=f"{border_style} bold"),
                Text(f"exit_code: {task_result.exit_code} · duration: {task_result.duration_text}", style="white"),
            ]
            if task_result.new_files:
                lines.append(Text(f"new_files: {', '.join(task_result.new_files)}", style="green"))
            if task_result.changed_files:
                lines.append(Text(f"changed_files: {', '.join(task_result.changed_files)}", style="yellow"))
            if task_result.error_text:
                lines.append(Text(f"error: {task_result.error_text[:300]}", style="red"))
            self.console.print(Panel(Group(*lines), title="Task Summary", border_style=border_style, padding=(1, 2)))
            return
        lines = [
            f"provider: {self._provider_label(task_result.provider)}",
            f"exit_code: {task_result.exit_code}",
            f"duration: {task_result.duration_text}",
        ]
        if task_result.new_files:
            lines.append(f"new_files: {', '.join(task_result.new_files)}")
        if task_result.changed_files:
            lines.append(f"changed_files: {', '.join(task_result.changed_files)}")
        if task_result.error_text:
            lines.append(f"error: {task_result.error_text[:300]}")
        self.print_block("Task Summary", "\n".join(lines), border_style=border_style)

    def render_workspace_screen(
        self,
        title: str,
        provider: str,
        mode: str,
        cwd: str,
        remote_status,
        prompt: str = "",
        stream_lines: list[str] | None = None,
        stream_title: str = "Live Stream",
        summary_result=None,
        answer_text: str = "",
        extra_renderer=None,
        ):
        self.clear()
        renderable = self.build_workspace_renderable(
            title=title,
            provider=provider,
            mode=mode,
            cwd=cwd,
            prompt=prompt,
            remote_running=remote_status.is_running,
            stream_lines=stream_lines,
            stream_title=stream_title,
            summary_result=summary_result,
            answer_text=answer_text,
            extra_renderer=extra_renderer,
        )
        if self.console:
            self.console.print(renderable)
        else:
            # Fallback path keeps the current clear-and-render behavior.
            self.print_task_workspace(
                title=title,
                provider=provider,
                mode=mode,
                cwd=cwd,
                prompt=prompt,
                remote_running=remote_status.is_running,
            )
            self.print_stream_snapshot(stream_title, stream_lines or [], provider=provider)
            if extra_renderer is not None:
                extra_renderer()
            if summary_result is not None:
                self.print_task_result_summary(summary_result)
            if answer_text:
                self.print_line()
                self.print_line(answer_text[:5000])
            self.print_shell_footer()

    def build_workspace_renderable(
        self,
        title: str,
        provider: str,
        mode: str,
        cwd: str,
        prompt: str = "",
        remote_running: bool = False,
        stream_lines: list[str] | None = None,
        stream_title: str = "Live Stream",
        summary_result=None,
        answer_text: str = "",
        extra_renderer=None,
    ):
        if not self.console or not Panel or not Group:
            return None

        provider_style = self._provider_style(provider)
        compact_prompt = " ".join(prompt.split())
        if len(compact_prompt) > 220:
            compact_prompt = compact_prompt[:219] + "…"

        header = Panel.fit(
            Group(
                Text(f"{self._provider_label(provider)} · {mode}", style=f"{provider_style} bold"),
                Text(cwd, style="bright_black"),
                Text(f"remote: {'running' if remote_running else 'stopped'}", style="bright_black"),
                Text(""),
                Text("Prompt", style=f"{provider_style} bold"),
                Text(compact_prompt or "No prompt"),
            ),
            title=title,
            border_style=provider_style,
            padding=(1, 2),
        )

        stream_panel = Panel(
            "\n".join((stream_lines or [])[-10:]) or "Waiting for first event...",
            title=stream_title,
            border_style=provider_style,
            padding=(1, 2),
        )

        right_blocks = []

        if summary_result is not None:
            summary_lines = [
                Text(f"{self._provider_label(summary_result.provider)}", style=f"{provider_style} bold"),
                Text(
                    f"exit_code: {summary_result.exit_code} · duration: {summary_result.duration_text}",
                    style="white",
                ),
            ]
            if summary_result.new_files:
                summary_lines.append(Text(f"new_files: {', '.join(summary_result.new_files)}", style="green"))
            if summary_result.changed_files:
                summary_lines.append(Text(f"changed_files: {', '.join(summary_result.changed_files)}", style="yellow"))
            if summary_result.error_text:
                summary_lines.append(Text(f"error: {summary_result.error_text[:300]}", style="red"))
            right_blocks.append(
                Panel(
                    Group(*summary_lines),
                    title="Task Summary",
                    border_style=provider_style,
                    padding=(1, 2),
                )
            )

        if extra_renderer is None:
            if mode == "orchestrated":
                right_blocks.append(
                    Panel(
                        Text("Multi-agent orchestration active", style=provider_style),
                        title="Plan",
                        border_style=provider_style,
                        padding=(1, 2),
                    )
                )
        else:
            # Live mode can't reuse imperative print callbacks, so keep a placeholder panel.
            right_blocks.append(
                Panel(
                    Text("Execution plan visible in stream updates", style="bright_black"),
                    title="Plan",
                    border_style=provider_style,
                    padding=(1, 2),
                )
            )

        if answer_text:
            right_blocks.append(
                Panel(
                    answer_text[:2200],
                    title="Answer",
                    border_style=provider_style,
                    padding=(1, 2),
                )
            )

        footer = Text(
            "Use /help for commands · /new to clear the workspace · /remote-control to start Telegram access",
            style="bright_black",
        )

        if Layout:
            layout = Layout()
            layout.split_column(
                Layout(header, name="header", size=9),
                Layout(name="body"),
                Layout(footer, name="footer", size=1),
            )
            layout["body"].split_row(
                Layout(stream_panel, name="stream", ratio=2),
                Layout(Group(*right_blocks) if right_blocks else Text(""), name="side", ratio=1),
            )
            return layout

        blocks = [header, stream_panel]
        blocks.extend(right_blocks)
        blocks.append(footer)
        return Group(*blocks)

    def start_live_workspace(self, **kwargs):
        if not self.supports_live():
            return None
        live = Live(self.build_workspace_renderable(**kwargs), console=self.console, refresh_per_second=8, transient=False)
        live.start()
        return live

    def update_live_workspace(self, live, **kwargs):
        if live is None:
            self.render_workspace_screen(**kwargs)
            return
        live.update(self.build_workspace_renderable(**kwargs), refresh=True)

    def stop_live_workspace(self, live):
        if live is not None:
            live.stop()

    def print_plan(self, plan):
        if self.console and Table:
            table = Table(title="Execution Plan")
            table.add_column("#", style="bold")
            table.add_column("Title")
            table.add_column("Provider")
            table.add_column("Kind")
            for index, subtask in enumerate(plan.subtasks, start=1):
                table.add_row(str(index), subtask.title, subtask.suggested_provider, subtask.task_kind)
            self.console.print(f"[bold]complexity[/bold]: {plan.complexity}")
            self.console.print(f"[bold]strategy[/bold]: {plan.strategy}")
            self.console.print(table)
            return

        self.print_kv("complexity", plan.complexity)
        self.print_kv("strategy", plan.strategy)
        self.print_line()
        for index, subtask in enumerate(plan.subtasks, start=1):
            self.print_line(f"{index}. {subtask.title} [{subtask.suggested_provider}]")
            self.print_line(f"   kind: {subtask.task_kind}")
            self.print_line(f"   reason: {subtask.reason}")

    def print_run_brief(self, run, index: int | None = None):
        prefix = f"{index}. " if index is not None else ""
        if self.console and Table:
            table = Table(show_header=False)
            table.add_column("key", style="bold")
            table.add_column("value")
            table.add_row("run", f"{prefix}{run.status_emoji} {run.run_id}")
            table.add_row("mode", run.mode)
            table.add_row("status", run.status)
            table.add_row("duration", run.duration_text)
            table.add_row("providers", run.provider_summary or "mixed")
            if run.model_summary:
                table.add_row("models", run.model_summary)
            if run.transport_summary:
                table.add_row("transport", run.transport_summary)
            self.console.print(table)
            return

        self.print_line(f"{prefix}{run.status_emoji} {run.run_id}")
        self.print_line(f"   mode: {run.mode}")
        self.print_line(f"   status: {run.status}")
        self.print_line(f"   duration: {run.duration_text}")
        self.print_line(f"   providers: {run.provider_summary or 'mixed'}")
        if run.model_summary:
            self.print_line(f"   models: {run.model_summary}")
        if run.transport_summary:
            self.print_line(f"   transport: {run.transport_summary}")

    def print_run_detail(self, run):
        self.print_kv("run_id", run.run_id)
        self.print_kv("status", run.status)
        self.print_kv("mode", run.mode)
        self.print_kv("complexity", run.complexity)
        if run.provider_summary:
            self.print_kv("providers", run.provider_summary)
        if run.model_summary:
            self.print_kv("models", run.model_summary)
        if run.transport_summary:
            self.print_kv("transport", run.transport_summary)
        if run.total_input_tokens or run.total_output_tokens:
            self.print_kv("tokens", f"{run.total_input_tokens} in / {run.total_output_tokens} out")
        if run.strategy:
            self.print_kv("strategy", run.strategy)
        if run.synthesis_provider:
            detail = run.synthesis_provider
            if run.synthesis_model:
                detail += f" · {run.synthesis_model}"
            if run.synthesis_transport:
                detail += f" [{run.synthesis_transport}]"
            self.print_kv("synthesis", detail)
        if run.review_provider:
            detail = run.review_provider
            if run.review_model:
                detail += f" · {run.review_model}"
            if run.review_transport:
                detail += f" [{run.review_transport}]"
            self.print_kv("review", detail)
        if run.artifact_file:
            self.print_kv("artifact", run.artifact_file)
        self.print_line()
        self.print_line("prompt:")
        self.print_line(run.prompt)
        if run.subtasks:
            self.print_line()
            self.print_line("subtasks:")
            for item in run.subtasks:
                details = f"{item.provider}"
                if item.model_name:
                    details += f" · {item.model_name}"
                if item.transport:
                    details += f" [{item.transport}]"
                if item.input_tokens or item.output_tokens:
                    details += f" · {item.input_tokens}/{item.output_tokens} tok"
                self.print_line(f"- {item.subtask_id}: {item.title} [{details}] ({item.status})")
        if run.review_answer:
            self.print_line()
            self.print_line("review:")
            self.print_line(run.review_answer[:3000])
        if run.answer_text:
            self.print_line()
            self.print_line("answer:")
            self.print_line(run.answer_text[:5000])

    def print_artifacts(self, artifacts: list[Path]):
        if not artifacts:
            self.print_line("No artifacts found.")
            return
        for item in artifacts:
            self.print_line(str(item))

    # ── Inline streaming (claude/codex/qwen style) ──────────────────────────

    def print_task_header(self, provider: str, cwd: str, prompt: str):
        """Minimal task header printed before streaming starts."""
        style = self._provider_style(provider)
        short = " ".join(prompt.split())
        if len(short) > 120:
            short = short[:119] + "…"
        if self.console:
            self.console.print()
            self.console.print(
                f"[{style}]◆[/] [bold]{provider}[/bold]  [bright_black]{cwd}[/bright_black]"
            )
            self.console.print(f"  [bright_black]{short}[/bright_black]")
            self.console.print()
            return
        accent = self._ansi_provider_color(provider)
        reset = self._ansi_reset()
        dim = self._ansi_dim()
        print()
        print(f"{accent}◆{reset} {provider}  {dim}{cwd}{reset}")
        print(f"  {dim}{short}{reset}")
        print()

    # ── stream event label helpers ────────────────────────────────────────────

    @staticmethod
    def _strip_emoji_prefix(line: str) -> str:
        """Remove leading emoji + optional 'Using: ' prefix."""
        # Strip up to two chars of emoji + optional space
        for prefix in (
            "🔧 Using: ", "🔧 ", "✏️ ", "📂 ", "👁️ ",
            "🐚 ", "⚙️ ", "💬 ", "🧠 ", "🏁 ",
        ):
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return line.strip()

    @staticmethod
    def _tool_label(line: str) -> str:
        """Extract a short tool/file label for the ● indicator."""
        raw = CliUi._strip_emoji_prefix(line)
        # "Using: <tool>(<args>)" or "Read(path/to/file.py)" → keep as-is
        return (raw[:60] + "…") if len(raw) > 60 else raw

    def print_stream_event(self, line: str, provider: str = "", thinking_mode: str = "compact"):
        """Print a single real-time stream event inline."""
        style = self._provider_style(provider) if provider else "cyan"

        # ── plain-text fallback ───────────────────────────────────────────────
        if not self.console:
            if line.startswith("💬 "):
                print(line[len("💬 "):].strip())
            elif line.startswith("🧠 ") and thinking_mode != "off":
                rendered = render_thinking_text(extract_thinking_chunk(line), thinking_mode, rich=False)
                if rendered:
                    print(rendered)
            elif line.startswith(("🔧", "⚙️", "📂", "✏️", "👁️", "🐚")):
                label = self._tool_label(line)
                print(f"  ● {label}")
            elif line.startswith("🐚 "):
                print(f"  $ {self._strip_emoji_prefix(line)}")
            return

        # ── Rich path ─────────────────────────────────────────────────────────

        if line.startswith("💬 "):
            # AI text output — render inline with basic markdown
            text = line[len("💬 "):].strip()
            self.console.print(_render_md_inline(text), markup=True)

        elif line.startswith("🧠 "):
            rendered = render_thinking_text(extract_thinking_chunk(line), thinking_mode, rich=True)
            if rendered:
                self.console.print(rendered)

        # write / create
        elif line.startswith(("✏️ ", "📂 ")):
            label = self._tool_label(line)
            self.console.print(f"  [{style}]◆[/] [bright_black]{label}[/bright_black]")

        # read / view
        elif line.startswith("👁️ "):
            label = self._tool_label(line)
            self.console.print(f"  [bright_black]○ {label}[/bright_black]")

        # shell command
        elif line.startswith("🐚 "):
            cmd = self._strip_emoji_prefix(line)
            # strip Russian/English "Running: " prefix
            for pfx in ("Running: ",):
                if cmd.startswith(pfx):
                    cmd = cmd[len(pfx):]
                    break
            self.console.print(f"  [{style}]$[/] [bright_black]{cmd}[/bright_black]")

        # tool use (generic)
        elif line.startswith("🔧 "):
            label = self._tool_label(line)
            self.console.print(f"  [bright_black]● {label}[/bright_black]")

        # init / misc
        elif line.startswith("⚙️ "):
            text = self._strip_emoji_prefix(line)
            self.console.print(f"  [bright_black]{text}[/bright_black]")

        # completion marker — suppress, footer handles it
        elif line.startswith("🏁 "):
            pass

        elif line.startswith(("❌ ", "✅ ")):
            self.console.print(f"  [bright_black]{line.strip()}[/bright_black]")

    def print_task_result_inline(self, result):
        """
        Print task result footer after inline streaming.

        Text output was already shown in real-time via print_stream_event;
        we only add: error text, file diffs, and a one-line summary footer.
        """
        style = self._provider_style(result.provider)
        ok = result.exit_code == 0

        # Build footer parts: ✓ provider · Xs · N tokens · M files changed
        tok_total = (getattr(result, "total_input_tokens", 0) or 0) + (
            getattr(result, "total_output_tokens", 0) or 0
        )
        files_n = len(result.new_files) + len(result.changed_files)
        duration = getattr(result, "duration_text", "") or ""
        footer_parts: list[str] = []
        if duration:
            footer_parts.append(duration)
        if tok_total:
            footer_parts.append(
                f"{tok_total:,} tok" if tok_total < 10_000 else f"{tok_total / 1_000:.1f}k tok"
            )
        if files_n:
            noun = "file" if files_n == 1 else "files"
            footer_parts.append(f"{files_n} {noun} changed")

        if self.console:
            # file diffs
            for f in result.new_files:
                self.print_file_diff(f, is_new=True, provider=result.provider)
            for f in result.changed_files:
                self.print_file_diff(f, is_new=False, provider=result.provider)
            if result.error_text:
                self.console.print()
                self.console.print(f"  [red]✗[/red] [red]{result.error_text[:300]}[/red]")
            # footer
            self.console.print()
            status_icon = f"[{style}]✓[/]" if ok else "[red]✗[/red]"
            provider_str = f"[{style}]{result.provider}[/]"
            meta = f"  [bright_black]{('  ·  '.join(footer_parts))}[/bright_black]" if footer_parts else ""
            self.console.print(f"  {status_icon} {provider_str}{meta}")
            return

        # plain fallback
        accent = self._ansi_provider_color(result.provider)
        reset = self._ansi_reset()
        dim = self._ansi_dim()
        for f in result.new_files:
            self.print_file_diff(f, is_new=True, provider=result.provider)
        for f in result.changed_files:
            self.print_file_diff(f, is_new=False, provider=result.provider)
        if result.error_text:
            print(f"\n  ✗ {result.error_text[:300]}")
        icon = "✓" if ok else "✗"
        meta = ("  " + "  ·  ".join(footer_parts)) if footer_parts else ""
        print(f"\n  {accent}{icon} {result.provider}{reset}{dim}{meta}{reset}")

    def print_orchestration_step_header(
        self, index: int, total: int, title: str, provider: str, cwd: str
    ):
        """Print a step separator for orchestration mode."""
        style = self._provider_style(provider)
        if self.console:
            self.console.print()
            from rich.rule import Rule as _Rule
            self.console.print(
                _Rule(
                    f"[{style}] Step {index}/{total}: {title} [{provider}] [/]",
                    style=style,
                )
            )
            self.console.print(f"  [bright_black]{cwd}[/bright_black]")
            self.console.print()
            return
        accent = self._ansi_provider_color(provider)
        reset = self._ansi_reset()
        width = min(self._terminal_width(), 120)
        label = f" Step {index}/{total}: {title} [{provider}] "
        dashes = "─" * max(0, (width - len(label)) // 2)
        print()
        print(f"{accent}{dashes}{label}{dashes}{reset}")
        print(f"  {cwd}")
        print()

    def print_orchestration_label(self, label: str, provider: str, cwd: str):
        """Print a synthesis/review step separator."""
        style = self._provider_style(provider)
        if self.console:
            self.console.print()
            from rich.rule import Rule as _Rule
            self.console.print(_Rule(f"[{style}] {label} [{provider}] [/]", style=style))
            self.console.print(f"  [bright_black]{cwd}[/bright_black]")
            self.console.print()
            return
        accent = self._ansi_provider_color(provider)
        reset = self._ansi_reset()
        width = min(self._terminal_width(), 120)
        label_str = f" {label} [{provider}] "
        dashes = "─" * max(0, (width - len(label_str)) // 2)
        print()
        print(f"{accent}{dashes}{label_str}{dashes}{reset}")
        print()

    def print_orchestration_subtask_result(self, subtask):
        """Print a per-subtask result after it finishes."""
        style = self._provider_style(subtask.provider)
        icon = "✓" if subtask.status in ("success", "reused") else "✗"
        indent = "  " * min(getattr(subtask, "depth", 0), 3)
        if self.console:
            self.console.print()
            self.console.print(
                f"  {indent}[{style}]{icon}[/] [{style}]{subtask.provider}[/]  [bright_black]{subtask.title}[/bright_black]"
            )
            for f in subtask.new_files[:6]:
                self.print_file_diff(f, is_new=True, provider=subtask.provider)
            for f in subtask.changed_files[:6]:
                self.print_file_diff(f, is_new=False, provider=subtask.provider)
            return
        accent = self._ansi_provider_color(subtask.provider)
        reset = self._ansi_reset()
        print(f"  {indent}{accent}{icon} {subtask.provider}{reset}  {subtask.title}")
        for f in subtask.new_files[:6]:
            self.print_file_diff(f, is_new=True, provider=subtask.provider)
        for f in subtask.changed_files[:6]:
            self.print_file_diff(f, is_new=False, provider=subtask.provider)

    # ── live status bar ───────────────────────────────────────────────────────

    def _status_renderable(self, action: str, elapsed: float, tokens: int, provider: str):
        if not Text:
            return None
        style = self._provider_style(provider)
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        tok_str = f"↑ {tokens / 1000:.1f}k" if tokens >= 1000 else f"↑ {tokens}"
        t = Text()
        t.append("◆ ", style=style)
        t.append(action, style="white")
        t.append(f"  ({time_str} · {tok_str} tokens)", style="bright_black")
        return t

    def start_status_bar(self, provider: str) -> tuple:
        """Returns (live_or_none, start_time, state_dict)."""
        start = _time.monotonic()
        state: dict = {"action": "Starting…", "tokens": 0}
        if not (self.console and Live and Text):
            return None, start, state
        renderable = self._status_renderable("Starting…", 0.0, 0, provider)
        live = Live(renderable, console=self.console, refresh_per_second=8, transient=True)
        live.start()
        return live, start, state

    def refresh_status_bar(self, live, start: float, state: dict, provider: str):
        if live is None:
            return
        elapsed = _time.monotonic() - start
        live.update(self._status_renderable(state["action"], elapsed, state["tokens"], provider))

    def stop_status_bar(self, live):
        if live is not None:
            live.stop()

    def pause_status_bar(self, live):
        """Temporarily stop the live display (before an interactive prompt)."""
        if live is not None:
            live.stop()

    def resume_status_bar(self, live, start: float, state: dict, provider: str):
        """Restart a previously paused live display."""
        if live is not None:
            live.start()
            self.refresh_status_bar(live, start, state, provider)

    # ── interactive prompts ───────────────────────────────────────────────────

    def prompt_secret(self, label: str, hint: str = "") -> str:
        """
        Draw a styled password-entry UI and return the typed secret.

        Modelled after Claude CLI / Gemini CLI:
          ◆ <label>
            <hint>
            Secret › ****
        """
        from cli.prompt import read_masked  # local import to avoid circular

        if self.console and Panel and Text:
            hint_line = f"\n  [bright_black]{hint}[/bright_black]" if hint else ""
            self.console.print(
                Panel(
                    f"[bright_black]{hint or 'Input is hidden.'}[/bright_black]",
                    title=f"[bold]{label}[/bold]",
                    border_style="green",
                    padding=(0, 2),
                )
            )
            display_prompt = "  › "
        else:
            accent = self._ansi_provider_color("openrouter")
            reset = self._ansi_reset()
            dim = self._ansi_dim()
            if hint:
                print(f"{dim}  {hint}{reset}")
            display_prompt = f"{accent}  {label} ›{reset} "

        return read_masked(display_prompt)

    def prompt_confirm(self, question: str, default: bool = False) -> bool:
        """
        Draw a styled yes/no confirmation and return the boolean answer.
        """
        from cli.prompt import read_confirm

        yn_hint = "[Y/n]" if default else "[y/N]"

        if self.console and Panel:
            self.console.print(
                Panel(
                    f"{question}",
                    title="[bold yellow]Confirmation[/bold yellow]",
                    border_style="yellow",
                    padding=(0, 2),
                )
            )
            display_prompt = f"  {yn_hint} › "
        else:
            accent = "\033[33m"
            reset = self._ansi_reset()
            dim = self._ansi_dim()
            print(f"{accent}  {question}{reset}")
            display_prompt = f"  {yn_hint} › "

        return read_confirm(display_prompt, default=default)

    def prompt_question(self, question: str, hint: str = "") -> str | None:
        """
        Draw a styled free-text question panel (used for model interaction).
        Returns the typed answer, or None if the user pressed Enter/Ctrl-C.
        """
        from cli.prompt import read_text

        if self.console and Panel:
            body = question
            if hint:
                body += f"\n[bright_black]{hint}[/bright_black]"
            self.console.print(
                Panel(
                    body,
                    title="[bold cyan]❓ Model asks[/bold cyan]",
                    border_style="cyan",
                    padding=(0, 2),
                )
            )
            display_prompt = "  › "
        else:
            dim = self._ansi_dim()
            reset = self._ansi_reset()
            print(f"\n  ❓ {question}")
            if hint:
                print(f"{dim}  {hint}{reset}")
            display_prompt = "  › "

        return read_text(display_prompt)

    # ── file diff / preview ───────────────────────────────────────────────────

    def print_file_diff(self, file_path: str, is_new: bool, provider: str = ""):
        """Show inline code diff or new-file preview."""
        path = Path(file_path)
        if not path.exists():
            return
        style = self._provider_style(provider)

        try:
            file_size = path.stat().st_size
        except OSError:
            return

        # ── rich path ────────────────────────────────────────────────────────
        if self.console and Syntax:
            # header line
            try:
                n_lines = len(path.read_text(errors="replace").splitlines()) if file_size < 500_000 else 0
            except Exception:
                n_lines = 0
            size_note = f"  [bright_black]({n_lines} lines)[/bright_black]" if n_lines else ""
            icon = "[green]+[/green]" if is_new else "[yellow]~[/yellow]"
            self.console.print(f"\n  {icon} [bold]{path.name}[/bold]{size_note}")

            if file_size > 200_000:
                self.console.print("  [bright_black](file too large to preview)[/bright_black]")
                return

            # git diff for modified files
            if not is_new:
                diff = _git_diff(path)
                if diff:
                    # strip the diff header lines, keep hunks
                    hunk_lines = []
                    for ln in diff.splitlines():
                        if ln.startswith(("diff ", "index ", "--- ", "+++ ")):
                            continue
                        hunk_lines.append(ln)
                    shown = "\n".join(hunk_lines[:60])
                    if len(hunk_lines) > 60:
                        shown += f"\n  ... ({len(hunk_lines) - 60} more lines)"
                    self.console.print(
                        Syntax(shown, "diff", theme="github-dark", word_wrap=False),
                        style="on #0d1117",
                    )
                    return
                # no git diff — show file content
                try:
                    content = path.read_text(errors="replace")
                    lines = content.splitlines()
                    shown = "\n".join(lines[:40])
                    if len(lines) > 40:
                        shown += f"\n# ... ({len(lines) - 40} more lines)"
                    self.console.print(
                        Syntax(shown, _detect_lang(path), theme="github-dark",
                               line_numbers=True, start_line=1, word_wrap=False),
                    )
                except Exception:
                    pass
                return

            # new file — show with + prefix as a diff
            try:
                content = path.read_text(errors="replace")
                lines = content.splitlines()
                shown_lines = lines[:50]
                shown = "\n".join(f"+ {l}" for l in shown_lines)
                if len(lines) > 50:
                    shown += f"\n+ ... ({len(lines) - 50} more lines)"
                self.console.print(
                    Syntax(shown, "diff", theme="github-dark", word_wrap=False),
                    style="on #0d1117",
                )
            except Exception:
                pass
            return

        # ── plain fallback ───────────────────────────────────────────────────
        accent = self._ansi_provider_color(provider)
        reset = self._ansi_reset()
        dim = self._ansi_dim()
        icon = "+" if is_new else "~"
        print(f"\n  {accent}{icon} {path.name}{reset}")
        if file_size > 200_000:
            print(f"  {dim}(file too large to preview){reset}")
            return
        if not is_new:
            diff = _git_diff(path)
            if diff:
                for ln in diff.splitlines()[:40]:
                    print(f"  {ln}")
                return
        try:
            content = path.read_text(errors="replace")
            lines = content.splitlines()
            prefix = "+ " if is_new else "  "
            for ln in lines[:30]:
                print(f"  {dim}{prefix}{ln}{reset}")
            if len(lines) > 30:
                print(f"  {dim}... ({len(lines) - 30} more lines){reset}")
        except Exception:
            pass
