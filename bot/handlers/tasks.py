"""
Task execution: run_task, run_orchestrated_task and their result rendering.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from core.task_models import ChatSession, SubtaskRun, TaskRun
from core.orchestrator import OrchestrationPlan

from bot.formatting import (
    build_task_buttons,
    chunk_code_sections,
    format_task_result_sections,
    send_answer_chunks,
    send_or_edit_structured,
)
from bot.streaming import TelegramStreamRenderer

if TYPE_CHECKING:
    from aiogram.types import Message
    from bot.core import BotCore


async def run_task(
    core: "BotCore",
    session: ChatSession,
    provider_name: str,
    prompt: str,
    message: "Message",
    status_msg: "Message",
) -> None:
    result = await core.execute_task(session, provider_name, prompt, status_msg)
    session.last_task_result = result
    runtime = core.get_runtime(session, provider_name)

    if result.exit_code == 0:
        answer_chunks = (
            chunk_code_sections(result.answer_text, runtime.parser._escape_html)
            if result.answer_text and result.answer_text.strip()
            else []
        )
        sections = format_task_result_sections(
            session.file_mgr.get_working_dir(),
            new_files=result.new_files or None,
            changed_files=result.changed_files or None,
        )
        if answer_chunks:
            sections.extend(["<b>📋 Ответ агента</b>", answer_chunks[0]])
        core.remember_task_result(session, result)
        keyboard = build_task_buttons(
            session.file_mgr.get_working_dir(),
            result.new_files,
            result.changed_files,
        )
        if sections:
            await send_or_edit_structured(core.bot, message, status_msg, sections, reply_markup=keyboard)
        else:
            await core.safe_edit(status_msg, "✅ <b>Задача выполнена.</b>", reply_markup=keyboard)
        await send_answer_chunks(
            core.bot, message, result.answer_text,
            runtime.parser._escape_html, skip_first_chunk=bool(answer_chunks),
        )
        return

    failure = runtime.manager.health.last_failure
    lines = [f"⚠️ <b>{core.provider_label(provider_name)} завершился с кодом {result.exit_code}</b>"]
    if failure:
        lines.append(f"<b>Причина:</b> <code>{escape(failure.short_label)}</code>")
        lines.append(escape(failure.message))
        if failure.retry_at:
            lines.append(f"<b>Доступность:</b> примерно после <code>{escape(failure.retry_at)}</code>")
    core.remember_task_result(session, result)
    await send_or_edit_structured(core.bot, message, status_msg, lines)


async def run_orchestrated_task(
    core: "BotCore",
    session: ChatSession,
    plan: OrchestrationPlan,
    message: "Message",
    status_msg: "Message",
    resume_from: int = 0,
    prior_subtasks: list[SubtaskRun] | None = None,
) -> None:
    first_provider = (
        plan.subtasks[resume_from].suggested_provider
        if resume_from < len(plan.subtasks)
        else session.current_provider
    )
    renderer = TelegramStreamRenderer(core, status_msg, first_provider, session)
    try:
        task_run, _ = await core.orchestrator.run_orchestrated_task(
            session=session,
            plan=plan,
            status_callback=lambda text: core.safe_edit(status_msg, text),
            resume_from=resume_from,
            prior_subtasks=prior_subtasks,
            stream_event_callback=renderer.on_stream_event,
            interaction_callback=renderer.on_interaction,
        )
    finally:
        await renderer.finalize()

    status_line = (
        "<b>🧭 Оркестрация завершена</b>"
        if task_run.status == "success"
        else "<b>⚠️ Оркестрация завершена с ошибками</b>"
    )
    sections = [
        status_line,
        f"<code>{escape(plan.prompt)}</code>",
        (
            f"<b>Сложность:</b> <code>{escape(plan.complexity)}</code>\n"
            f"<b>Стратегия:</b> {escape(plan.strategy)}\n"
            f"<b>Статус:</b> <code>{escape(task_run.status)}</code>\n"
            f"<b>Synthesis:</b> <code>{escape(task_run.synthesis_provider or '-')}</code>\n"
            f"<b>Review:</b> <code>{escape(task_run.review_provider or '-')}</code>"
        ),
        "<b>Subtasks</b>\n" + "\n".join(
            f"• <code>{escape(st.subtask_id)}</code> — {escape(st.title)} "
            f"(<b>{escape(st.provider)}</b>, {escape(st.status)})"
            for st in task_run.subtasks
        ),
    ]
    if task_run.handoff_artifacts:
        sections.append(
            "<b>Handoff artifacts</b>\n"
            + "\n\n".join(
                f"<pre>{escape(item[:1200])}</pre>"
                for item in task_run.handoff_artifacts[:3]
            )
        )
    sections.extend(
        format_task_result_sections(
            session.file_mgr.get_working_dir(),
            new_files=task_run.new_files or None,
            changed_files=task_run.changed_files or None,
        )
    )
    if task_run.error_text:
        sections.append(f"<b>❌ Ошибка</b>\n<pre>{escape(task_run.error_text[:3000])}</pre>")
    if task_run.review_answer:
        sections.append(f"<b>🔍 Review</b>\n<pre>{escape(task_run.review_answer[:3000])}</pre>")

    keyboard = build_task_buttons(
        session.file_mgr.get_working_dir(),
        task_run.new_files,
        task_run.changed_files,
        can_retry_failed=(task_run.mode == "orchestrated" and task_run.status in {"failed", "partial"}),
    )
    await send_or_edit_structured(core.bot, message, status_msg, sections, reply_markup=keyboard)

    if task_run.answer_text.strip():
        last_provider = task_run.subtasks[-1].provider if task_run.subtasks else session.current_provider
        runtime = core.get_runtime(session, last_provider)
        await send_answer_chunks(core.bot, message, task_run.answer_text, runtime.parser._escape_html)
