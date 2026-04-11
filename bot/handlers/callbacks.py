"""
Inline keyboard callback handler: dispatch_callback
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, FSInputFile

from core.providers import is_supported_provider, normalize_provider_name
from core.task_models import ChatSession, TaskRun

from bot.file_registry import resolve as _resolve_file

from bot.formatting import (
    build_file_preview_messages,
    format_task_result_sections,
    send_answer_chunks,
    send_or_edit_structured,
)

if TYPE_CHECKING:
    from bot.core import BotCore


async def dispatch_callback(core: "BotCore", callback: CallbackQuery) -> None:
    data = callback.data or ""
    if not callback.message:
        await callback.answer()
        return
    session = core.get_session(callback.message.chat.id)

    # ── interaction resolution ──────────────────────────────────────────────
    if data.startswith("interaction:"):
        answer_key = data.split(":", 1)[1]
        renderer = core.get_active_renderer(session.chat_id)
        if renderer is None:
            await callback.answer("Prompt already expired", show_alert=True)
            return
        answer_map = {"yes": "y", "no": "n", "skip": ""}
        renderer.resolve_interaction(answer_map.get(answer_key, ""))
        await callback.answer("Response received")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # ── file view ───────────────────────────────────────────────────────────
    if data.startswith("view_file:"):
        fid = data.split(":", 1)[1]
        resolved = _resolve_file(fid)
        fp = Path(resolved) if resolved else None
        if fp is None or not fp.exists() or not fp.is_file():
            await callback.answer("File not found", show_alert=True)
            return
        if fp.stat().st_size > 50_000:
            await callback.message.answer_document(FSInputFile(fp, filename=fp.name))
            await callback.answer()
        else:
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                active = session.active_provider or session.current_provider
                escape_fn = core.get_runtime(session, active).parser._escape_html
                for preview in build_file_preview_messages(fp, content, escape_fn):
                    await callback.message.answer(preview)
                await callback.answer()
            except Exception as exc:
                await callback.answer(f"Error: {exc}", show_alert=True)
        return

    # ── task provider switch (before start) ─────────────────────────────────
    if data.startswith("task_provider:"):
        provider = data.split(":", 1)[1].strip().lower()
        if not is_supported_provider(provider):
            await callback.answer("Unknown provider", show_alert=True)
            return
        task = session.pending_tasks.get(callback.message.message_id)
        if task is None:
            await callback.answer("This task has already completed", show_alert=True)
            return
        if task.mode == "orchestrated":
            await callback.answer("Orchestrated tasks use providers defined by the plan", show_alert=True)
            return
        if task.started:
            await callback.answer("Task already started", show_alert=True)
            return
        task.provider = provider
        await callback.answer(f"Provider set to {provider}")
        pos = core.queue_position(session, task)
        status = (
            f"⏳ <b>Starting {core.provider_label(provider)}…</b>"
            if pos <= 1
            else f"⏳ <b>Task queued.</b>\nProvider: <b>{escape(provider)}</b>\nPosition: {pos}"
        )
        await core.safe_edit(
            callback.message, status,
            reply_markup=core._task_provider_keyboard(provider),
            parse_mode=ParseMode.HTML,
        )
        return

    # ── set default provider ─────────────────────────────────────────────────
    if data.startswith("set_provider:"):
        provider = data.split(":", 1)[1].strip().lower()
        if not is_supported_provider(provider):
            await callback.answer("Unknown provider", show_alert=True)
            return
        session.current_provider = normalize_provider_name(provider)
        core.session_store.save(session)
        await callback.answer(f"Provider switched to {provider}")
        from core.providers import list_supported_provider_names
        provider_cmds = ", ".join(f"<code>/provider {n}</code>" for n in list_supported_provider_names())
        await core.safe_edit(
            callback.message,
            (
                f"🤖 Default provider: <b>{escape(session.current_provider)}</b>\n"
                f"▶️ Active: <b>{escape(session.active_provider or session.current_provider)}</b>\n"
                f"🕘 Queued: {core.queued_count(session)}\n\n"
                f"Switch: {provider_cmds}."
            ),
            reply_markup=core.provider_keyboard(session),
            parse_mode=ParseMode.HTML,
        )
        return

    # ── repeat last task ─────────────────────────────────────────────────────
    if data == "repeat_task":
        if session.last_task_run and session.last_task_run.mode == "orchestrated" and session.last_task_run.prompt:
            plan = await core.orchestrator.build_plan(session, session.last_task_run.prompt)
            session.last_plan = plan
            core.session_store.save(session)
            await callback.answer("Plan re-queued")
            first = plan.subtasks[0].suggested_provider if plan.subtasks else session.current_provider
            await core.enqueue_task(
                session, first, session.last_task_run.prompt,
                callback.message, "⏳ <b>Repeating orchestration plan…</b>",
                mode="orchestrated", plan=plan,
            )
            return
        if not session.last_task_result.prompt:
            await callback.answer("No previous task", show_alert=True)
            return
        await callback.answer("Task queued")
        await core.enqueue_task(
            session,
            session.last_task_result.provider or session.current_provider,
            session.last_task_result.prompt,
            callback.message,
            "⏳ <b>Repeating last task…</b>",
        )
        return

    # ── retry failed subtask ─────────────────────────────────────────────────
    if data == "retry_failed_subtask":
        last_run = session.last_task_run
        if not last_run or last_run.mode != "orchestrated":
            await callback.answer("No orchestration task to retry", show_alert=True)
            return
        retry_index = core.orchestrator.find_retry_start_index(last_run)
        if retry_index is None:
            await callback.answer("No failed subtasks", show_alert=True)
            return
        plan = session.last_plan or await core.orchestrator.build_plan(session, last_run.prompt)
        session.last_plan = plan
        core.session_store.save(session)
        provider = (
            plan.subtasks[retry_index].suggested_provider
            if retry_index < len(plan.subtasks)
            else (last_run.synthesis_provider or session.current_provider)
        )
        await callback.answer(f"Retrying from step {retry_index + 1}")
        await core.enqueue_task(
            session, provider, last_run.prompt, callback.message,
            f"⏳ <b>Resuming orchestrator from step {retry_index + 1}…</b>",
            mode="orchestrated", plan=plan,
            resume_from=retry_index, prior_subtasks=last_run.subtasks,
        )
        return

    # ── show details ─────────────────────────────────────────────────────────
    if data == "show_details":
        if not session.last_task_result.has_details:
            await callback.answer("No details available yet", show_alert=True)
            return
        await callback.answer()
        last_run = session.last_task_run or TaskRun.from_task_result(session.last_task_result)
        sections = [
            f"<b>📝 Last task</b>\n<code>{escape(session.last_task_result.prompt)}</code>",
            f"<b>🤖 Provider:</b> <code>{escape(session.last_task_result.provider)}</code>",
        ]
        if last_run.strategy:
            sections.append(f"<b>Strategy:</b> {escape(last_run.strategy)}")
        if last_run.synthesis_provider:
            sections.append(f"<b>Synthesis:</b> <code>{escape(last_run.synthesis_provider)}</code>")
        if last_run.review_provider:
            sections.append(f"<b>Review:</b> <code>{escape(last_run.review_provider)}</code>")
        if last_run.subtasks:
            sections.append(
                "<b>Subtasks</b>\n" + "\n".join(
                    f"• <code>{escape(st.subtask_id)}</code> — {escape(st.title)} ({escape(st.provider)})"
                    for st in last_run.subtasks
                )
            )
        if last_run.handoff_artifacts:
            sections.append(
                "<b>Handoff artifacts</b>\n" + "\n\n".join(
                    f"<pre>{escape(a[:1200])}</pre>" for a in last_run.handoff_artifacts[:3]
                )
            )
        if last_run.review_answer:
            sections.append(f"<b>🔍 Review</b>\n<pre>{escape(last_run.review_answer[:3000])}</pre>")
        sections.extend(format_task_result_sections(
            session.file_mgr.get_working_dir(),
            new_files=session.last_task_result.new_files or None,
            changed_files=session.last_task_result.changed_files or None,
        ))
        if session.last_task_result.touched_files:
            sections.append(
                "<b>📁 Touched files</b>\n"
                + "\n".join(
                    f"• <code>{escape(Path(p).name)}</code>"
                    for p in session.last_task_result.touched_files[:10]
                )
            )
        await send_or_edit_structured(core.bot, callback.message, callback.message, sections)
        prov = session.last_task_result.provider or session.current_provider
        escape_fn = core.get_runtime(session, prov).parser._escape_html
        await send_answer_chunks(
            core.bot, callback.message,
            session.last_task_result.answer_text, escape_fn,
        )
        return

    # ── plan actions ──────────────────────────────────────────────────────────
    if data == "plan_run":
        plan = session.last_plan
        if plan is None or not plan.prompt.strip():
            await callback.answer("Plan not found", show_alert=True)
            return
        await callback.answer("Plan queued")
        first = plan.subtasks[0].suggested_provider if plan.subtasks else session.current_provider
        await core.enqueue_task(
            session, first, plan.prompt, callback.message,
            "⏳ <b>Starting orchestrator…</b>",
            mode="orchestrated", plan=plan,
        )
        return

    if data == "plan_edit":
        plan = session.last_plan
        plan_prompt = escape(plan.prompt) if plan else "new task"
        await callback.answer()
        await callback.message.answer(
            "✏️ To refine the plan, send:\n"
            f"<code>/plan {plan_prompt}</code>"
        )
        return

    if data == "plan_cancel":
        await callback.answer("Plan cancelled")
        await core.safe_edit(
            callback.message,
            "🛑 <b>Plan cancelled.</b>\n\n"
            "You can send <code>/plan &lt;task&gt;</code> or "
            "<code>/orchestrate &lt;task&gt;</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    await callback.answer()
