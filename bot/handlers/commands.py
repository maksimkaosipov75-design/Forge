"""
Informational and session management commands:
/start, /help, /commands, /status, /limits, /usage, /metrics,
/cancel, /clear, /compact, /todos
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from core.providers import list_supported_provider_names, supported_provider_commands_text
from core.task_models import ChatSession, TaskResult, TaskRun

from bot.formatting import chunk_code_sections, send_or_edit_structured

if TYPE_CHECKING:
    from aiogram.types import Message
    from bot.core import BotCore


async def handle_start(core: "BotCore", message: "Message", session: ChatSession) -> None:
    if any(rt.manager.is_running for rt in session.runtimes.values()):
        await message.answer("🤖 Agent is running. Send your tasks!")
    else:
        await core.ensure_runtime_started(session, session.current_provider)
        await message.answer(
            f"🤖 <b>Multi-Agent Remote Control</b>\n\n"
            f"Default provider: <b>{escape(session.current_provider)}</b>\n\n"
            "Send a message — the agent will execute your task.\n\n"
            "Use /help for a list of commands."
        )


async def handle_help(core: "BotCore", message: "Message") -> None:
    await message.answer(
        "📋 <b>Available commands:</b>\n\n"
        "<b>Files:</b>\n"
        "/ls [path] — list directory\n"
        "/cat &lt;file&gt; — show file contents\n"
        "/tree [path] — directory tree\n"
        "/cd &lt;path&gt; — change directory\n"
        "/pwd — current directory\n\n"
        "<b>Projects:</b>\n"
        "/project &lt;name&gt; &lt;path&gt; — save project\n"
        "/load &lt;name&gt; — load project\n"
        "/projects — list projects\n\n"
        "<b>Providers:</b>\n"
        "/provider — current provider\n"
        "/agents — provider selector buttons\n"
        f"/provider {supported_provider_commands_text()} — switch provider\n"
        "/qwen &lt;task&gt;, /codex &lt;task&gt;, /claude &lt;task&gt;\n"
        "/status — status and progress\n"
        "/limits — rate limits and availability\n"
        "/metrics — internal metrics\n"
        "/history — recent tasks\n"
        "/runs — recent runs\n"
        "/artifacts 1 — run artifact\n"
        "/diff — diff of latest changes\n"
        "/review [focus] — code review of last result\n"
        "/commit [message] — git commit\n"
        "/plan &lt;task&gt; — build orchestration plan\n"
        "/orchestrate &lt;task&gt; — run plan\n"
        "/retry_failed — resume from last failure\n"
        "/recover — restore from checkpoint (after crash)\n"
        "/cancel — cancel current task\n"
        "/btw &lt;question&gt; — ask the agent a question\n"
        "/! &lt;cmd&gt; — pass command directly to the agent CLI\n"
        "/clear — reset session\n"
        "/help — this help"
    )


async def handle_commands(core: "BotCore", message: "Message") -> None:
    await message.answer(
        "📚 <b>Commands</b>\n\n"
        "<b>General</b>\n/help, /commands, /status, /limits, /provider, /agents\n\n"
        "<b>Tasks</b>\n/plan, /orchestrate, /retry_failed, /recover, /btw, /!, /qwen, /codex, /claude\n\n"
        "<b>History &amp; artifacts</b>\n/history, /runs, /artifacts 1, /diff, /todos, /usage, /metrics, /review\n\n"
        "<b>Session</b>\n/clear, /cancel, /compact [N|filter]\n\n"
        "<b>Models &amp; providers</b>\n/model, /model &lt;provider&gt;, /model &lt;provider&gt; &lt;model&gt;\n/reset-provider, /commit\n\n"
        "<b>Files</b>\n/pwd, /ls, /tree, /cat, /cd"
    )


async def handle_status(core: "BotCore", message: "Message", session: ChatSession) -> None:
    active = session.active_provider or session.current_provider
    runtime = core.get_runtime(session, active)
    lines = [f"<b>📡 Provider health</b>"]
    for name in list_supported_provider_names():
        rt = core.get_runtime(session, name)
        lines.extend(rt.health.summary_lines())
        lines.append("")
    provider_block = "\n".join(lines[:-1])
    await message.answer(
        f"🤖 Provider: <b>{escape(active)}</b>\n"
        + runtime.parser.get_status_text()
        + f"\n🕘 Queued: {session.task_queue.qsize()}"
        + "\n\n"
        + provider_block
        + "\n\n"
        + session.file_mgr.get_project_context()
    )


async def handle_limits(core: "BotCore", message: "Message", session: ChatSession) -> None:
    sections = ["<b>⏱️ Rate limits &amp; availability</b>"]
    for name in list_supported_provider_names():
        rt = core.get_runtime(session, name)
        sections.extend(rt.health.summary_lines())
    sections.append(
        "<i>If the CLI cannot report exact quota data, the last known failure reason is shown.</i>"
    )
    await core.send_structured(message, sections)


async def handle_usage(core: "BotCore", message: "Message", session: ChatSession) -> None:
    sections = ["<b>📊 Session usage</b>"]
    for name in list_supported_provider_names():
        rt = session.runtimes.get(name)
        stats = session.provider_stats.get(name)
        if rt:
            last_in, last_out, total_in, total_out = rt.parser.get_token_usage()
        else:
            last_in = last_out = total_in = total_out = 0
        tasks = stats.total_tasks if stats else 0
        success = stats.successful_tasks if stats else 0
        fail = stats.failed_tasks if stats else 0
        model = session.provider_models.get(name, "").strip()
        if not model and rt:
            model = getattr(rt.manager, "model_name", "") or ""
        model = model or "default"
        sections.append(
            f"<b>{escape(name)}</b>\n"
            f"Model: <code>{escape(model)}</code>\n"
            f"Tasks: {tasks} • ok: {success} • err: {fail}\n"
            f"Last: <code>{last_in}</code> in / <code>{last_out}</code> out\n"
            f"Total: <code>{total_in}</code> in / <code>{total_out}</code> out"
        )
    sections.append(
        "<i>Token counts come from the stream. Zero if the provider did not emit usage events.</i>"
    )
    await core.send_structured(message, sections)


async def handle_metrics(core: "BotCore", message: "Message") -> None:
    payload = core._render_metrics()
    sections = [
        "<b>📈 Metrics</b>",
        f"<pre>{escape(payload[:3500])}</pre>",
    ]
    if len(payload) > 3500:
        sections.append("<i>Output truncated. Full version available at HTTP endpoint /metrics.</i>")
    await core.send_structured(message, sections)


async def handle_todos(core: "BotCore", message: "Message", session: ChatSession) -> None:
    todos: list[str] = []
    for line in (session.last_task_result.answer_text or "").splitlines():
        s = line.strip()
        if s.startswith(("- [ ] ", "* [ ] ", "- [x] ", "* [x] ", "TODO:", "Todo:", "todo:")):
            todos.append(s)
    if not todos:
        await message.answer("📝 No TODO list found in the last response.")
    else:
        await core.send_structured(message, ["<b>📝 TODOs from last response</b>", *todos[:20]])


async def handle_cancel(core: "BotCore", message: "Message", session: ChatSession) -> None:
    for rt in session.runtimes.values():
        await rt.manager.stop()
        rt.parser.clear_full_buffer()
    dropped = core.clear_pending_queue(session)
    extra = f" Cleared from queue: {dropped}." if dropped else ""
    await message.answer(f"🛑 Task cancelled.{extra}")


async def handle_clear(core: "BotCore", message: "Message", session: ChatSession) -> None:
    for rt in session.runtimes.values():
        await rt.manager.stop()
        rt.parser.clear_full_buffer()
    session.last_task_result = TaskResult(provider=session.current_provider)
    session.last_task_run = None
    dropped = core.clear_pending_queue(session)
    session.history.clear()
    session.run_history.clear()
    session.last_plan = None
    core.session_store.clear(session.chat_id)
    extra = f" Removed from queue: {dropped}." if dropped else ""
    await message.answer(f"🗑 Session reset.{extra}")


async def handle_compact(
    core: "BotCore", message: "Message", session: ChatSession, arg: str
) -> None:
    if arg.isdigit():
        keep = max(1, int(arg))
        session.history = session.history[-keep:]
        session.run_history = session.run_history[-keep:]
        if session.history:
            session.last_task_result = session.history[-1]
        if session.run_history:
            session.last_task_run = session.run_history[-1]
        core.session_store.save(session)
        await message.answer(f"🗜 History compacted. Kept: <b>{keep}</b>.")
    elif arg:
        needle = arg.lower()
        session.history = [
            i for i in session.history
            if needle in i.prompt.lower() or needle in i.answer_text.lower()
        ]
        session.run_history = [
            i for i in session.run_history
            if needle in i.prompt.lower() or needle in i.answer_text.lower()
        ]
        if session.history:
            session.last_task_result = session.history[-1]
        if session.run_history:
            session.last_task_run = session.run_history[-1]
        core.session_store.save(session)
        await message.answer(
            f"🗜 History filtered by <code>{escape(arg)}</code>. "
            f"Tasks: {len(session.history)}, runs: {len(session.run_history)}."
        )
    else:
        keep = 3
        session.history = session.history[-keep:]
        session.run_history = session.run_history[-keep:]
        if session.history:
            session.last_task_result = session.history[-1]
        if session.run_history:
            session.last_task_run = session.run_history[-1]
        core.session_store.save(session)
        await message.answer(f"🗜 History compacted to last <b>{keep}</b> entries.")
