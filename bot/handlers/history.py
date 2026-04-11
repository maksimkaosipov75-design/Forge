"""
History and artifact commands: /history, /runs, /artifacts, /diff
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from core.task_models import ChatSession, TaskRun

from bot.formatting import (
    build_file_preview_messages,
    chunk_code_sections,
    format_task_result_sections,
    send_answer_chunks,
    send_or_edit_structured,
)

if TYPE_CHECKING:
    from aiogram.types import Message
    from bot.core import BotCore


def _history_lines(session: ChatSession, limit: int = 5) -> list[str]:
    lines = ["<b>🕘 Recent tasks</b>"]
    items = session.run_history or [TaskRun.from_task_result(i) for i in session.history]
    for idx, item in enumerate(reversed(items[-limit:]), start=1):
        ts = escape(item.finished_or_started_at.replace("T", " ")[:19])
        preview = escape(item.prompt[:120] + ("…" if len(item.prompt) > 120 else ""))
        lines.append(
            f"{idx}. {item.status_emoji} <code>{preview}</code>\n"
            f"   <i>{ts}</i> • {item.duration_text} • subtasks: {len(item.subtasks)} • files: {len(item.touched_files)}"
        )
    return lines


def _runs_lines(session: ChatSession, limit: int = 10) -> list[str]:
    lines = ["<b>🏃 Recent runs</b>"]
    for idx, item in enumerate(reversed(session.run_history[-limit:]), start=1):
        ts = escape(item.finished_or_started_at.replace("T", " ")[:19])
        lines.append(
            f"{idx}. {item.status_emoji} <code>{escape(item.run_id)}</code>\n"
            f"   <i>{ts}</i> • {item.duration_text} • mode: <code>{escape(item.mode)}</code>"
        )
    return lines


async def handle_history_list(
    core: "BotCore", message: "Message", session: ChatSession
) -> None:
    if not session.run_history and not session.history:
        await message.answer("🕘 Task history is empty.")
        return
    lines = _history_lines(session)
    lines.append("\n<i>To view a record: /history 1</i>")
    await message.answer("\n".join(lines))


async def handle_history_detail(
    core: "BotCore", message: "Message", session: ChatSession, index: int
) -> None:
    source = session.run_history or [TaskRun.from_task_result(i) for i in session.history]
    recent = list(reversed(source))
    if index < 1 or index > len(recent):
        await message.answer("❌ No history record with that number.")
        return
    item = recent[index - 1]
    sections = [
        f"<b>🕘 Task #{index}</b>",
        f"<code>{escape(item.prompt)}</code>",
        (
            f"<i>{escape(item.finished_or_started_at.replace('T', ' ')[:19])}</i> • "
            f"{item.duration_text} • {item.status_emoji} • <b>{escape(item.provider_summary or 'mixed')}</b>"
        ),
    ]
    if item.strategy:
        sections.append(f"<b>Strategy:</b> {escape(item.strategy)}")
    if item.synthesis_provider:
        sections.append(f"<b>Synthesis:</b> <code>{escape(item.synthesis_provider)}</code>")
    if item.review_provider:
        sections.append(f"<b>Review:</b> <code>{escape(item.review_provider)}</code>")
    if item.subtasks:
        sections.append(
            "<b>Subtasks</b>\n"
            + "\n".join(
                f"• <code>{escape(st.subtask_id)}</code> — {escape(st.title)} "
                f"(<b>{escape(st.provider)}</b>, {escape(st.status)})"
                for st in item.subtasks
            )
        )
    if item.handoff_artifacts:
        sections.append(
            "<b>Handoff artifacts</b>\n"
            + "\n\n".join(
                f"<pre>{escape(a[:1200])}</pre>" for a in item.handoff_artifacts[:3]
            )
        )
    sections.extend(format_task_result_sections(
        session.file_mgr.get_working_dir(),
        new_files=item.new_files or None,
        changed_files=item.changed_files or None,
    ))
    if item.error_text:
        sections.append(f"<b>❌ Error</b>\n<pre>{escape(item.error_text[:3000])}</pre>")
    if item.review_answer:
        sections.append(f"<b>🔍 Review</b>\n<pre>{escape(item.review_answer[:3000])}</pre>")
    await core.send_structured(message, sections)

    prov = item.subtasks[0].provider if item.subtasks else session.current_provider
    escape_fn = core.get_runtime(session, prov).parser._escape_html
    await send_answer_chunks(
        core.bot, message, item.answer_text, escape_fn,
        title="<b>📋 Response from history</b>",
    )


async def handle_runs(
    core: "BotCore", message: "Message", session: ChatSession
) -> None:
    if not session.run_history:
        await message.answer("🏃 No runs yet.")
        return
    lines = _runs_lines(session)
    lines.append("\n<i>To view an artifact: /artifacts 1</i>")
    await message.answer("\n".join(lines))


async def handle_artifact(
    core: "BotCore", message: "Message", session: ChatSession, index: int
) -> None:
    recent = list(reversed(session.run_history))
    if index < 1 or index > len(recent):
        await message.answer("❌ No run with that number.")
        return
    run = recent[index - 1]
    artifact_path = Path(run.artifact_file) if run.artifact_file else None
    if not artifact_path or not artifact_path.exists():
        await message.answer("⚠️ No artifact found for this run.")
        return
    content = artifact_path.read_text(encoding="utf-8", errors="replace")
    active = session.active_provider or session.current_provider
    escape_fn = core.get_runtime(session, active).parser._escape_html
    for preview in build_file_preview_messages(artifact_path, content, escape_fn)[:3]:
        await message.answer(preview)


async def handle_diff(
    core: "BotCore", message: "Message", session: ChatSession, mode: str = "last"
) -> None:
    work_dir = session.file_mgr.get_working_dir()
    rc, _, _ = await core.run_git(work_dir, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        await message.answer("⚠️ Current directory is not a git repository.")
        return

    diff_files = session.last_task_result.touched_files if mode == "last" else []
    stat_args = ["diff", "--stat"] + (["--", *diff_files] if diff_files else [])
    rc, stat_out, stat_err = await core.run_git(work_dir, *stat_args)
    if rc != 0:
        await message.answer(f"❌ <pre>{escape(stat_err[:1500])}</pre>")
        return

    patch_args = ["diff", "--", *diff_files] if diff_files else ["diff"]
    rc, diff_out, diff_err = await core.run_git(work_dir, *patch_args)
    if rc != 0:
        await message.answer(f"❌ <pre>{escape(diff_err[:1500])}</pre>")
        return

    if not diff_out.strip():
        await message.answer("🟢 No changes to show.")
        return

    sections = ["<b>🧾 Diff of recent changes</b>"]
    if session.last_task_result.prompt:
        sections.append(f"<code>{escape(session.last_task_result.prompt[:160])}</code>")
    if stat_out.strip():
        sections.append(f"<pre>{escape(stat_out[:3000])}</pre>")
    await core.send_structured(message, sections)

    if mode == "stat":
        return

    prov = session.last_task_result.provider or session.current_provider
    escape_fn = core.get_runtime(session, prov).parser._escape_html
    diff_chunks = chunk_code_sections(diff_out, escape_fn, language="diff", max_len=2800)
    for idx, chunk in enumerate(diff_chunks):
        title = "<b>🧾 Patch</b>" if idx == 0 else "<b>🧾 Patch</b> <i>(continued)</i>"
        await message.answer(f"{title}\n\n{chunk}")
