"""Generate SVG screenshots for README using Rich console."""
from pathlib import Path
from rich.console import Console

OUT = Path(__file__).parent / "images"
OUT.mkdir(exist_ok=True)

W = 110  # terminal columns


def make_console() -> Console:
    return Console(
        record=True,
        width=W,
        force_terminal=True,
        force_jupyter=False,
        highlight=False,
        markup=True,
    )


def p(c: Console, markup: str = ""):
    c.print(markup)


SEP  = "[dim]" + "─  " * 22 + "[/dim]"
SEP2 = "[dim]" + "─  " * 14 + "[/dim]"


# ─────────────────────────────────────────────────────────────────
# 1. Welcome screen
# ─────────────────────────────────────────────────────────────────
def gen_welcome():
    c = make_console()

    p(c, "  [bold #b07cff]◆[/bold #b07cff] [bold white]Forge[/bold white]  [dim]v0.1  ·  qwen[/dim]")
    p(c)
    p(c, SEP)
    p(c)
    p(c, "  [bold #b07cff]qwen[/bold #b07cff]       [green]up[/green]")
    p(c, "  [bold #6aa7ff]codex[/bold #6aa7ff]      [dim]ready[/dim]")
    p(c, "  [bold #ff9e57]claude[/bold #ff9e57]     [dim]ready[/dim]")
    p(c)
    p(c, SEP)
    p(c)
    p(c, "  [bold dim]Recent runs[/bold dim]")
    p(c, "  [green]✔[/green]  single     [bold #b07cff]qwen[/bold #b07cff]    refactor auth middleware")
    p(c, "  [green]✔[/green]  single     [bold #b07cff]qwen[/bold #b07cff]    add unit tests for parser")
    p(c, "  [green]✔[/green]  orchestr   [bold #ff9e57]mixed[/bold #ff9e57]   build REST API with tests")
    p(c, "  [yellow]⚠[/yellow]  single     [bold #6aa7ff]codex[/bold #6aa7ff]   cd '/projects/myapp'")
    p(c)
    p(c, SEP)
    p(c)
    p(c, "  [bold dim]Commands[/bold dim]  [dim](type / to open dropdown)[/dim]")
    p(c, "  [bold #b07cff]/help            [/bold #b07cff][dim]help                [/dim]  [bold #b07cff]/commands        [/bold #b07cff][dim]all commands[/dim]")
    p(c, "  [bold #b07cff]/provider        [/bold #b07cff][dim]switch provider     [/dim]  [bold #b07cff]/model           [/bold #b07cff][dim]change model[/dim]")
    p(c, "  [bold #b07cff]/plan            [/bold #b07cff][dim]preview orchestr    [/dim]  [bold #b07cff]/run-plan        [/bold #b07cff][dim]execute preview[/dim]")
    p(c, "  [bold #b07cff]/orchestrate     [/bold #b07cff][dim]multi-agent run     [/dim]  [bold #b07cff]/review          [/bold #b07cff][dim]review last result[/dim]")
    p(c, "  [bold #b07cff]/runs            [/bold #b07cff][dim]run history         [/dim]  [bold #b07cff]/remote-control  [/bold #b07cff][dim]Telegram access[/dim]")
    p(c)
    p(c, SEP)
    p(c, "  [dim][bold]/[/bold] command menu   [bold]Tab[/bold] autocomplete   "
        "[bold]@file.py[/bold] inline file   [bold]@provider:[/bold]prompt   "
        "[bold]Shift+Enter[/bold] multi-line   [bold]Ctrl+F[/bold] search[/dim]")
    p(c)
    p(c, "  [bold #b07cff]◆[/bold #b07cff]  [dim]qwen  ·  qwen3-coder-plus  ·  ~/projects/myapp [main]  ·  ctx 0[/dim]")

    (OUT / "forge-welcome.svg").write_text(c.export_svg(title="forge"))
    print("✔ forge-welcome.svg")


# ─────────────────────────────────────────────────────────────────
# 2. Orchestration
# ─────────────────────────────────────────────────────────────────
def gen_orchestration():
    c = make_console()

    p(c, "  [bold #b07cff]◆[/bold #b07cff] [bold white]Forge[/bold white]  [dim]v0.1  ·  qwen  ·  orchestrated[/dim]")
    p(c)
    p(c, SEP)
    p(c, "  [dim]>[/dim] [white]build a REST API with auth, tests, and OpenAPI docs[/white]  [dim](AI)[/dim]")
    p(c, "  [dim]orchestrate  ·  ~/projects/myapp[/dim]")
    p(c)
    p(c, "  strategy: parallel")
    p(c, "  [dim]Split into independent layers — auth, routes, tests — then synthesize.[/dim]")
    p(c, "  [dim]1. Implement JWT auth middleware [qwen][/dim]")
    p(c, "  [dim]2. Build route handlers with validation [codex][/dim]")
    p(c, "  [dim]3. Write integration tests [codex][/dim]")
    p(c, "  [dim]4. Generate OpenAPI docs [claude][/dim]")
    p(c)
    p(c, SEP2)
    p(c, "[bold #b07cff]▶[/bold #b07cff] [bold]Step 1/4[/bold]  Implement JWT auth middleware  [dim][qwen][/dim]")
    p(c, "  [dim]~/projects/myapp[/dim]")
    p(c, "  [dim]✏️  auth/middleware.py  auth/tokens.py  auth/__init__.py[/dim]")
    p(c)
    p(c, SEP2)
    p(c, "[bold #6aa7ff]▶[/bold #6aa7ff] [bold]Step 2/4[/bold]  Build route handlers with validation  [dim][codex][/dim]")
    p(c, "  [dim]~/projects/myapp[/dim]")
    p(c, "  [dim]👁️  reading  auth/middleware.py[/dim]")
    p(c, "  [dim]✏️  writing  api/routes.py  api/schemas.py[/dim]")
    p(c)
    p(c, SEP2)
    p(c, "[bold #6aa7ff]▶[/bold #6aa7ff] [bold]Step 3/4[/bold]  Write integration tests  [dim][codex][/dim]")
    p(c, "  [dim]~/projects/myapp[/dim]")
    p(c, "  [dim]🐚  $ pytest tests/ -q --tb=short[/dim]")
    p(c, "  [dim]✏️  tests/test_auth.py  tests/test_routes.py[/dim]")
    p(c)
    p(c, SEP2)
    p(c, "[bold #ff9e57]▶[/bold #ff9e57] [bold]Synthesis[/bold]")
    p(c)
    p(c, "  ✅ Done  [dim]·  mixed  ·  1m 48s  ·  7 files changed[/dim]")
    p(c)
    p(c, "  [bold #b07cff]⠸[/bold #b07cff] Step 3/4··  [dim]qwen3-coder-plus[/dim]  [dim](52s · ↑ 1.4k tokens)[/dim]")

    (OUT / "forge-orchestration.svg").write_text(c.export_svg(title="forge — orchestration"))
    print("✔ forge-orchestration.svg")


# ─────────────────────────────────────────────────────────────────
# 3. Live streaming
# ─────────────────────────────────────────────────────────────────
def gen_streaming():
    c = make_console()

    p(c, "  [bold #ff9e57]◆[/bold #ff9e57] [bold white]Forge[/bold white]  [dim]v0.1  ·  claude[/dim]")
    p(c)
    p(c, SEP)
    p(c, "  [dim]>[/dim] [white]refactor the auth module to use bcrypt and add rate limiting[/white]")
    p(c)
    p(c, "  I'll refactor the auth module to use bcrypt for password hashing")
    p(c, "  and add rate limiting to prevent brute-force attacks.")
    p(c)
    p(c, "  Let me start by reading the current implementation:")
    p(c)
    p(c, "  [dim]👁️  reading  auth/middleware.py  auth/tokens.py[/dim]")
    p(c)
    p(c, "  The current implementation uses MD5 — I'll replace it with bcrypt")
    p(c, "  and add a sliding-window rate limiter per IP.")
    p(c)
    p(c, "  [dim]✏️  writing  auth/middleware.py[/dim]")
    p(c)
    p(c, "[dim]  import bcrypt[/dim]")
    p(c, "[dim]  from collections import defaultdict[/dim]")
    p(c, "[dim]  import time[/dim]")
    p(c)
    p(c, "[dim]  RATE_LIMIT = 5    # attempts[/dim]")
    p(c, "[dim]  WINDOW     = 60   # seconds[/dim]")
    p(c, "[dim]  _attempts: dict[str, list[float]] = defaultdict(list)[/dim]")
    p(c)
    p(c, "  [dim]✏️  writing  auth/tokens.py[/dim]")
    p(c, "  [dim]🐚  $ pytest tests/auth/ -q[/dim]")
    p(c)
    p(c, "  All 12 tests pass.")
    p(c)
    p(c, "  ✅ Done  [dim]·  claude  ·  38s  ·  2 files changed[/dim]")
    p(c)
    p(c, "  [bold #ff9e57]⠋[/bold #ff9e57] Writing···  [dim]claude-sonnet-4-6[/dim]  [dim](38s · ↑ 847 tokens)[/dim]")

    (OUT / "forge-streaming.svg").write_text(c.export_svg(title="forge — live streaming"))
    print("✔ forge-streaming.svg")


# ─────────────────────────────────────────────────────────────────
# 4. /plan preview
# ─────────────────────────────────────────────────────────────────
def gen_plan():
    c = make_console()

    p(c, "  [bold #b07cff]◆[/bold #b07cff] [bold white]Forge[/bold white]  [dim]v0.1  ·  qwen[/dim]")
    p(c)
    p(c, SEP)
    p(c, "  [dim]>[/dim] [white]/plan add a GraphQL API layer on top of the existing REST endpoints[/white]")
    p(c)
    p(c, "  [bold #b07cff]◆[/bold #b07cff] [bold]Planning[dim]···[/dim][/bold]  [dim][qwen3-coder-plus][/dim]")
    p(c)
    p(c, "  ╭─ Plan ──────────────────────────────────────────────────────────╮")
    p(c, "  │  strategy    parallel                                           │")
    p(c, "  │  complexity  moderate  [dim](AI plan)[/dim]                              │")
    p(c, "  │  eta         ~2 min                                             │")
    p(c, "  │                                                                 │")
    p(c, "  │  [dim]Schema-first: define types → resolvers → wire into FastAPI.[/dim]   │")
    p(c, "  ╰─────────────────────────────────────────────────────────────────╯")
    p(c)
    p(c, "  [dim]  #   Task                                  Agent    Group[/dim]")
    p(c, "  [dim]  ─   ────────────────────────────────────  ───────  ─────[/dim]")
    p(c, "  [dim]  1   Define GraphQL schema types           claude[/dim]")
    p(c, "  [dim]  2   Implement query resolvers             qwen     ∥ A[/dim]")
    p(c, "  [dim]  3   Implement mutation resolvers          qwen     ∥ A[/dim]")
    p(c, "  [dim]  4   Wire Strawberry into FastAPI          codex[/dim]")
    p(c)
    p(c, "  Run this plan?  [bold #b07cff]Y[/bold #b07cff][dim]/n[/dim]  ·  or use [bold]/run-plan[/bold]  [dim]/edit-plan[/dim]")
    p(c)
    p(c, "  [bold #b07cff]◆[/bold #b07cff]  [dim]qwen  ·  qwen3-coder-plus  ·  ~/projects/myapp [main]  ·  ctx 4k[/dim]")

    (OUT / "forge-diff.svg").write_text(c.export_svg(title="forge — plan preview"))
    print("✔ forge-diff.svg  (plan preview)")


if __name__ == "__main__":
    gen_welcome()
    gen_orchestration()
    gen_streaming()
    gen_plan()
    print("\nAll screenshots written to", OUT)
