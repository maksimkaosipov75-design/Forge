"""Generate SVG screenshots for Forge README — matches real CLI output format."""
from rich.console import Console
from rich.text import Text
from pathlib import Path

OUT = Path(__file__).parent / "images"
OUT.mkdir(exist_ok=True)

SEP = "  " + "─  " * 34
SEP_SHORT = "  " + "─  " * 19


def make_console(width=102) -> Console:
    return Console(record=True, width=width, force_terminal=True, force_jupyter=False)


# ── Screenshot 0: Welcome screen ─────────────────────────────────────────────
def gen_welcome():
    c = make_console()

    c.print()
    c.print("  [bold white]◆ Forge[/bold white]  [dim]v0.1  ·  qwen  ·  …/projects/api  ·  main*[/dim]")
    c.print("  [dim]python · data · scripting[/dim]")
    c.print()
    c.print(f"  [dim]{SEP}[/dim]")
    c.print()
    c.print("  [#b07cff]qwen    [/#b07cff]  [green]up[/green]")
    c.print("  [#6aa7ff]codex   [/#6aa7ff]  [green]up[/green]")
    c.print("  [#ff9e57]claude  [/#ff9e57]  [green]up[/green]")
    c.print()
    c.print(f"  [dim]{SEP}[/dim]")
    c.print()
    c.print("  [bold dim]Recent runs[/bold dim]")
    c.print("  ✅  [dim]single    [/dim]  [#b07cff]qwen[/#b07cff]")
    c.print("  ✅  [dim]single    [/dim]  [#b07cff]qwen[/#b07cff]")
    c.print("  ✅  [dim]orchestrat[/dim]  [dim]mixed[/dim]")
    c.print()
    c.print(f"  [dim]{SEP}[/dim]")
    c.print()
    c.print("  [bold dim]Commands[/bold dim]  [dim](type / to open dropdown)[/dim]")
    c.print("  [#b07cff]/help              [/#b07cff][dim]help[/dim]"
            "  [#b07cff]/commands          [/#b07cff][dim]all commands[/dim]")
    c.print("  [#b07cff]/provider          [/#b07cff][dim]switch provider[/dim]"
            "  [#b07cff]/model             [/#b07cff][dim]change model[/dim]")
    c.print("  [#b07cff]/plan              [/#b07cff][dim]preview orchestration[/dim]"
            "  [#b07cff]/run-plan          [/#b07cff][dim]execute preview[/dim]")
    c.print("  [#b07cff]/orchestrate       [/#b07cff][dim]multi-agent run[/dim]"
            "  [#b07cff]/review            [/#b07cff][dim]review last result[/dim]")
    c.print("  [#b07cff]/runs              [/#b07cff][dim]run history[/dim]"
            "  [#b07cff]/remote-control    [/#b07cff][dim]Telegram access[/dim]")
    c.print()
    c.print(f"  [dim]{SEP}[/dim]")
    c.print(
        "  [dim][bold]/[/bold] command menu   "
        "[bold]Tab[/bold] autocomplete   "
        "[bold]@file.py[/bold] inline file   "
        "[bold]@provider:[/bold]prompt   "
        "[bold]Shift+Enter[/bold] multi-line   "
        "[bold]Ctrl+F[/bold] search[/dim]"
    )
    c.print()

    c.save_svg(str(OUT / "forge-welcome.svg"), title="Forge · Welcome")
    print("generated forge-welcome.svg")


# ── Screenshot 1: /plan with Y/N confirmation ─────────────────────────────────
def gen_orchestration():
    c = make_console()

    # Titlebar (dim, single line)
    c.print()
    c.print("  [dim]◆ Forge  v0.1  ·  qwen  ·  …/projects/api[/dim]")
    c.print()

    # Previous session
    c.print(f"  [dim]{SEP}[/dim]")
    c.print("  [dim]>[/dim] [dim]write unit tests for the auth module[/dim]")
    c.print("  [dim]qwen  ·  ~/projects/api[/dim]")
    c.print()
    c.print("  [dim]✓ Done  ·  qwen  ·  12s[/dim]")
    c.print()

    # New prompt
    c.print(f"  [dim]{SEP}[/dim]")
    c.print("  [dim]>[/dim] [white]build a REST API for user auth with JWT and SQLite[/white]")
    c.print("  [dim]qwen  ·  ~/projects/api[/dim]")
    c.print()

    # AI Plan panel — bordered with box-drawing chars (matches _plan_panel_lines)
    W = 88
    label = " AI Plan "
    dash_l = (W - 2 - len(label)) // 2
    dash_r = W - 2 - len(label) - dash_l
    c.print(f"[dim]╭{'─' * dash_l}[/dim][cyan]{label}[/cyan][dim]{'─' * dash_r}╮[/dim]")
    c.print(f"[dim]│[/dim]  [bold dim]{'strategy':<11}[/bold dim] split by layer — backend core first, then API surface{'':>12}[dim]│[/dim]")
    c.print(f"[dim]│[/dim]  [bold dim]{'complexity':<11}[/bold dim] medium  [dim]·[/dim]  ETA ≈ 90s{'':>38}[dim]│[/dim]")
    c.print(f"[dim]│[/dim]{' ' * (W - 2)}[dim]│[/dim]")
    c.print(f"[dim]│[/dim]  [bold dim]{'rationale':<11}[/bold dim] JWT and SQLite integration is straightforward but benefits{'':>7}[dim]│[/dim]")
    c.print(f"[dim]│[/dim]  {' ' * 13}from a clean separation: DB schema first, then handler logic.{'':>5}[dim]│[/dim]")
    c.print(f"[dim]╰{'─' * (W - 2)}╯[/dim]")
    c.print()

    # Subtask table (matches _subtask_table_lines)
    c.print("  [bold dim]#      subtask                       provider  depends on  group[/bold dim]")
    c.print("  [dim]─────────────────────────────────────────────────────────────[/dim]")
    c.print("  [dim]s1    [/dim]Database schema and models     [#b07cff]qwen      [/#b07cff][dim]—           0[/dim]")
    c.print("  [dim]s2    [/dim]JWT auth middleware             [#6aa7ff]codex     [/#6aa7ff][dim]s1          1[/dim]")
    c.print("  [dim]s3    [/dim]REST endpoints and OpenAPI docs [#ff9e57]claude    [/#ff9e57][dim]s2          2[/dim]")
    c.print()
    c.print("  Run this plan?  [#b07cff]Y[/#b07cff][dim]/n[/dim]  ·  or use [bold]/run-plan[/bold]  [dim]/edit-plan[/dim]")
    c.print()

    c.save_svg(str(OUT / "forge-orchestration.svg"), title="Forge · Plan & Confirmation")
    print("generated forge-orchestration.svg")


# ── Screenshot 2: Live streaming with op indicator ────────────────────────────
def gen_streaming():
    c = make_console()

    c.print()
    c.print("  [dim]◆ Forge  v0.1  ·  qwen  ·  …/projects/api[/dim]")
    c.print()

    # Previous session (dim)
    c.print(f"  [dim]{SEP}[/dim]")
    c.print("  [dim]>[/dim] [dim]write unit tests for the auth module[/dim]")
    c.print("  [dim]qwen  ·  ~/projects/api[/dim]")
    c.print()
    c.print("  [dim]✓ Done  ·  qwen  ·  12s  ·  3 files changed[/dim]")
    c.print()

    # Current session
    c.print(f"  [dim]{SEP}[/dim]")
    c.print("  [dim]>[/dim] [white]refactor session_store.py to use async SQLite[/white]")
    c.print("  [dim]qwen  ·  ~/projects/api[/dim]")
    c.print()

    # Op indicator — reading (dim, matches _op_indicator_text)
    c.print("  [dim]↳ reading 2 files  session_store.py, config.py[/dim]")
    c.print()

    # Streamed response lines
    c.print("  I'll refactor `session_store.py` to use `aiosqlite` for non-blocking I/O.")
    c.print()
    c.print("  Here's the plan:")
    c.print()
    c.print("  1. Replace `sqlite3.connect` with `aiosqlite.connect` context manager")
    c.print("  2. Convert `_init_db`, `_load_session_payload`, `_save_session_payload`")
    c.print("     and all checkpoint methods to `async def`")
    c.print("  3. Update `SessionStore.__init__` to schedule async init via")
    c.print("     `asyncio.get_event_loop().run_until_complete()`")
    c.print("  4. Keep the public API compatible — callers already `await` these methods")
    c.print()
    c.print("  [dim]Starting with the connection helper…[/dim]")
    c.print()

    # Op indicator — writing (green)
    c.print("  [green]↳ writing 1 file  session_store.py[/green]")
    c.print()
    c.print("  [bold cyan]▌[/bold cyan]")

    c.save_svg(str(OUT / "forge-streaming.svg"), title="Forge · Live Streaming")
    print("generated forge-streaming.svg")


# ── Screenshot 3: Diff with line numbers ──────────────────────────────────────
def gen_diff():
    c = make_console()

    c.print()
    c.print("  [dim]◆ Forge  v0.1  ·  qwen  ·  …/projects/api[/dim]")
    c.print()

    # Previous sessions (dim)
    c.print(f"  [dim]{SEP}[/dim]")
    c.print("  [dim]>[/dim] [dim]write unit tests for the auth module[/dim]")
    c.print("  [dim]qwen  ·  ~/projects/api[/dim]")
    c.print()
    c.print("  [dim]✓ Done  ·  qwen  ·  12s  ·  3 files changed[/dim]")
    c.print()

    c.print(f"  [dim]{SEP}[/dim]")
    c.print("  [dim]>[/dim] [white]refactor session_store.py to use async SQLite[/white]")
    c.print("  [dim]qwen  ·  ~/projects/api[/dim]")
    c.print()

    # Diff output — real _file_diff_text format: single num column + +/- marker
    c.print()
    c.print("  [yellow]~[/yellow] [bold]session_store.py[/bold]  [dim](89 lines)[/dim]")
    c.print()
    c.print("  [dim]@@  _connect[/dim]")
    c.print("  [dim]  24   [/dim][dim]    def __init__(self, sessions_root: Path):[/dim]")
    c.print("  [dim]  25   [/dim][dim]        self.sessions_root = sessions_root[/dim]")
    c.print("  [dim]  26   [/dim][dim]        self.sessions_root.mkdir(exist_ok=True)[/dim]")
    c.print("  [dim]  28   [/dim][dim]        self._init_db()[/dim]")
    c.print()
    c.print("  [dim]  29 [/dim][red]- def _connect(self) -> sqlite3.Connection:[/red]")
    c.print("  [dim]  30 [/dim][red]-     conn = sqlite3.connect(self.db_path)[/red]")
    c.print("  [dim]  31 [/dim][red]-     conn.row_factory = sqlite3.Row[/red]")
    c.print("  [dim]  32 [/dim][red]-     return conn[/red]")
    c.print()
    c.print("  [dim]  29 [/dim][#b07cff]+[/#b07cff] [#b07cff]async def _connect(self):[/#b07cff]")
    c.print("  [dim]  30 [/dim][#b07cff]+[/#b07cff] [#b07cff]    async with aiosqlite.connect(self.db_path) as conn:[/#b07cff]")
    c.print("  [dim]  31 [/dim][#b07cff]+[/#b07cff] [#b07cff]        conn.row_factory = aiosqlite.Row[/#b07cff]")
    c.print("  [dim]  32 [/dim][#b07cff]+[/#b07cff] [#b07cff]        yield conn[/#b07cff]")
    c.print()
    c.print("  [dim]  33   [/dim][dim]    def _init_db(self):[/dim]")
    c.print("  [dim]  34   [/dim][dim]        with self._connect() as conn:[/dim]")
    c.print()

    # Completion line
    c.print("  [#b07cff]✓[/#b07cff] Done  [dim]·  qwen  ·  8s[/dim]  [dim]·  1 file changed[/dim]")
    c.print()

    c.print()

    c.save_svg(str(OUT / "forge-diff.svg"), title="Forge · Diff View")
    print("generated forge-diff.svg")


gen_welcome()
gen_orchestration()
gen_streaming()
gen_diff()
print("done.")
