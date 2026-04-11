"""
Higher-level workflow commands:
/plan, /orchestrate, /retry_failed, /review, /commit, /btw
"""

from __future__ import annotations

import logging
from html import escape
from typing import TYPE_CHECKING

from task_models import ChatSession

from bot.formatting import (
    build_plan_preview_buttons,
    chunk_code_sections,
    send_answer_chunks,
    send_or_edit_structured,
)

if TYPE_CHECKING:
    from aiogram.types import Message
    from bot.core import BotCore

log = logging.getLogger(__name__)


# ── /plan ─────────────────────────────────────────────────────────────────────

async def handle_plan(
    core: "BotCore", message: "Message", session: ChatSession, prompt: str
) -> None:
    status_msg = await message.answer("⏳ <b>Строю план…</b>")
    plan = await core.orchestrator.build_plan(session, prompt)
    session.last_plan = plan
    core.session_store.save(session)

    sections = [
        "<b>🧭 План оркестрации</b>",
        f"<code>{escape(plan.prompt)}</code>",
        (
            f"<b>Сложность:</b> <code>{escape(plan.complexity)}</code>\n"
            f"<b>Стратегия:</b> {escape(plan.strategy)}"
            + (f"\n<i>{escape(plan.ai_rationale)}</i>" if plan.ai_rationale else "")
        ),
    ]
    for i, st in enumerate(plan.subtasks, start=1):
        deps = ", ".join(st.depends_on) if st.depends_on else "none"
        sections.append(
            f"<b>{i}. {escape(st.title)}</b>\n"
            f"<b>Тип:</b> <code>{escape(st.task_kind)}</code>\n"
            f"<b>Агент:</b> <code>{escape(st.suggested_provider)}</code>\n"
            f"<b>Depends on:</b> <code>{escape(deps)}</code>\n"
            f"{escape(st.description)}\n"
            f"<i>{escape(st.reason)}</i>"
        )
    eta = core.orchestrator.estimate_plan_eta(plan, session)
    sections.append(
        "Подтвердите кнопкой или отредактируйте командой "
        "<code>/plan &lt;обновлённая задача&gt;</code>."
    )
    sections.append(f"<b>Оценка времени:</b> <code>{escape(eta)}</code>")

    await send_or_edit_structured(
        core.bot, message, status_msg, sections,
        reply_markup=build_plan_preview_buttons(),
    )


# ── /orchestrate ──────────────────────────────────────────────────────────────

async def handle_orchestrate(
    core: "BotCore", message: "Message", session: ChatSession, prompt: str
) -> None:
    plan = await core.orchestrator.build_plan(session, prompt)
    session.last_plan = plan
    core.session_store.save(session)
    first_provider = plan.subtasks[0].suggested_provider if plan.subtasks else session.current_provider
    await core.enqueue_task(
        session, first_provider, prompt, message,
        "⏳ <b>Запускаю orchestrator…</b>",
        mode="orchestrated", plan=plan,
    )


# ── /retry_failed ─────────────────────────────────────────────────────────────

async def handle_retry_failed(
    core: "BotCore", message: "Message", session: ChatSession
) -> None:
    last_run = session.last_task_run
    if not last_run or last_run.mode != "orchestrated":
        await message.answer("⚠️ Последняя задача не была orchestration-run.")
        return
    retry_index = core.orchestrator.find_retry_start_index(last_run)
    if retry_index is None:
        await message.answer("🟢 В последнем orchestration-run нет упавшей подзадачи.")
        return
    plan = session.last_plan or await core.orchestrator.build_plan(session, last_run.prompt)
    session.last_plan = plan
    core.session_store.save(session)
    provider = (
        plan.subtasks[retry_index].suggested_provider
        if retry_index < len(plan.subtasks)
        else (last_run.synthesis_provider or session.current_provider)
    )
    await core.enqueue_task(
        session, provider, last_run.prompt, message,
        f"⏳ <b>Возобновляю orchestrator с шага {retry_index + 1}…</b>",
        mode="orchestrated", plan=plan,
        resume_from=retry_index, prior_subtasks=last_run.subtasks,
    )


# ── /review ───────────────────────────────────────────────────────────────────

def _build_review_request(
    task_prompt: str,
    answer_text: str,
    touched_files: list[str],
    review_focus: str = "",
) -> str:
    sections = [
        "You are reviewing the result of another coding agent.",
        "Be concise and practical.",
        "Report:",
        "1. Verdict",
        "2. Bugs or risks",
        "3. Missing tests or validation",
        "4. Recommended next step",
        "",
        "Original task:",
        task_prompt or "(unknown)",
        "",
        "Touched files:",
        "\n".join(f"- {item}" for item in touched_files[:20]) or "- none",
        "",
        "Result to review:",
        answer_text or "(empty result)",
    ]
    if review_focus.strip():
        sections.extend(["", f"Extra focus: {review_focus.strip()}"])
    return "\n".join(sections)


def _pick_review_provider(session: ChatSession, source: str, provider_paths: dict) -> str:
    for candidate in ("claude", "codex", "qwen"):
        if candidate != source and candidate in provider_paths:
            return candidate
    return session.current_provider


async def handle_review(
    core: "BotCore", message: "Message", session: ChatSession, focus: str = ""
) -> None:
    last = session.last_task_result
    if not last.answer_text.strip() and not last.touched_files:
        await message.answer("⚠️ Нечего отправлять на review: нет последнего результата.")
        return

    source = last.provider or session.current_provider
    reviewer = _pick_review_provider(session, source, core.provider_paths)
    await core.ensure_runtime_started(session, reviewer)
    runtime = core.get_runtime(session, reviewer)

    prompt = _build_review_request(last.prompt, last.answer_text, last.touched_files, focus)
    status_msg = await message.answer(
        "⏳ <b>Запускаю code review…</b>\n\n"
        f"<b>Источник:</b> <code>{escape(source)}</code>\n"
        f"<b>Reviewer:</b> <code>{escape(reviewer)}</code>"
    )

    prev_result = session.last_task_result
    prev_active = session.active_provider
    try:
        session.active_provider = reviewer
        result = await core.execute_task(
            session, reviewer, prompt, status_msg,
            status_prefix=(
                "⏳ <b>Выполняю review…</b>\n\n"
                f"<b>Reviewer:</b> <code>{escape(reviewer)}</code>"
            ),
        )
    finally:
        session.last_task_result = prev_result
        session.active_provider = prev_active

    if result.exit_code != 0:
        await send_or_edit_structured(
            core.bot, message, status_msg,
            [f"⚠️ <b>Review завершился с ошибкой</b>",
             f"<pre>{escape((result.error_text or 'Unknown error')[:3000])}</pre>"],
        )
        return

    if session.last_task_run:
        run = session.last_task_run
        run.review_provider = reviewer
        run.review_prompt = prompt
        run.review_answer = result.answer_text
        if not run.answer_text.strip():
            run.answer_text = prev_result.answer_text
        run.artifact_file = core.session_store.write_run_artifact(session, run)
    core.session_store.save(session)

    await send_or_edit_structured(
        core.bot, message, status_msg,
        ["<b>🔍 Review готов</b>",
         f"<b>Источник:</b> <code>{escape(source)}</code>\n<b>Reviewer:</b> <code>{escape(reviewer)}</code>"],
    )
    await send_answer_chunks(
        core.bot, message, result.answer_text,
        runtime.parser._escape_html, title="<b>🔍 Ответ reviewer-а</b>",
    )


# ── /commit ───────────────────────────────────────────────────────────────────

async def handle_commit(
    core: "BotCore", message: "Message", session: ChatSession, commit_msg: str = ""
) -> None:
    work_dir = session.file_mgr.get_working_dir()
    rc, _, _ = await core.run_git(work_dir, "rev-parse", "--is-inside-work-tree")
    if rc != 0:
        await message.answer("⚠️ Текущая директория не является git-репозиторием.")
        return

    rc, status_out, status_err = await core.run_git(work_dir, "status", "--short")
    if rc != 0:
        await message.answer(f"❌ Не удалось получить git status:\n<pre>{escape(status_err[:3000])}</pre>")
        return
    if not status_out.strip():
        await message.answer("🟢 Нет изменений для коммита.")
        return

    derived = commit_msg.strip() or session.last_task_result.prompt.strip() or "AI: update project"
    safe_msg = " ".join(derived.split())[:120] or "AI: update project"
    status_msg = await message.answer(f"⏳ <b>Создаю git commit…</b>\n\n<code>{escape(safe_msg)}</code>")

    rc, _, add_err = await core.run_git(work_dir, "add", "-A")
    if rc != 0:
        await send_or_edit_structured(core.bot, message, status_msg,
            [f"❌ <b>git add -A завершился с ошибкой</b>\n<pre>{escape(add_err[:3000])}</pre>"])
        return

    rc, commit_out, commit_err = await core.run_git(work_dir, "commit", "-m", safe_msg)
    if rc != 0:
        combined = (commit_err or commit_out or "Unknown git commit error")[:3000]
        await send_or_edit_structured(core.bot, message, status_msg,
            [f"❌ <b>git commit завершился с ошибкой</b>\n<pre>{escape(combined)}</pre>"])
        return

    rc, rev_out, _ = await core.run_git(work_dir, "rev-parse", "--short", "HEAD")
    hash_ = rev_out.strip() if rc == 0 else "unknown"
    sections = ["<b>✅ Commit создан</b>",
                f"<b>Hash:</b> <code>{escape(hash_)}</code>",
                f"<b>Message:</b> <code>{escape(safe_msg)}</code>"]
    if commit_out.strip():
        sections.append(f"<pre>{escape(commit_out[:3000])}</pre>")
    await send_or_edit_structured(core.bot, message, status_msg, sections)


# ── /recover ─────────────────────────────────────────────────────────────────


async def handle_recover(
    core: "BotCore", message: "Message", session: ChatSession
) -> None:
    """Resume an orchestration run that was interrupted mid-flight.

    Loads the most recent checkpoint written by the orchestrator after each
    subtask and re-enqueues the remaining work.
    """
    checkpoint = core.session_store.load_checkpoint(session)
    if checkpoint is None:
        await message.answer(
            "🟢 Нет сохранённого чекпоинта.\n\n"
            "Чекпоинт создаётся после каждого успешного шага оркестрации "
            "и очищается при успешном завершении."
        )
        return

    completed = [s for s in checkpoint.subtasks if s.status in {"success", "reused"}]
    failed = [s for s in checkpoint.subtasks if s.status == "failed"]
    resume_from = len(completed)

    plan = session.last_plan
    if plan is None:
        plan = await core.orchestrator.build_plan(session, checkpoint.prompt)
        session.last_plan = plan
        core.session_store.save(session)

    provider = (
        plan.subtasks[resume_from].suggested_provider
        if resume_from < len(plan.subtasks)
        else session.current_provider
    )

    failed_info = f", упал: {escape(failed[-1].title)}" if failed else ""
    await core.enqueue_task(
        session, provider, checkpoint.prompt, message,
        f"⏳ <b>Восстанавливаю с шага {resume_from + 1}"
        f"/{len(plan.subtasks)}{failed_info}…</b>",
        mode="orchestrated", plan=plan,
        resume_from=resume_from, prior_subtasks=checkpoint.subtasks,
    )


# ── /btw ──────────────────────────────────────────────────────────────────────

async def handle_btw(
    core: "BotCore", message: "Message", session: ChatSession, question: str
) -> None:
    if session.task_lock.locked() or not session.task_queue.empty():
        await message.answer("⏳ В этом чате уже есть активная или ожидающая задача.")
        return
    provider = session.current_provider
    runtime = core.get_runtime(session, provider)
    await core.ensure_runtime_started(session, provider)
    status_msg = await message.answer(f"❓ Спрашиваю: <i>{escape(question)}</i>")
    try:
        async with session.task_lock:
            session.active_provider = provider
            runtime.parser.clear_full_buffer()
            runtime.parser.set_final_result("")
            runtime.manager.set_final_result_callback(lambda t: runtime.parser.set_final_result(t))
            await runtime.manager.send_command(question, cwd=session.file_mgr.get_working_dir())
            response = runtime.parser.final_result or runtime.parser.get_full_response()
        if response and response.strip():
            sections = [
                f"<b>💬 Ответ ({escape(provider)})</b>",
                *chunk_code_sections(response, runtime.parser._escape_html),
            ]
            await send_or_edit_structured(core.bot, message, status_msg, sections)
        else:
            await core.safe_edit(status_msg, "⚠️ <b>Не удалось получить ответ.</b>")
    except Exception as exc:
        log.error("Ошибка /btw: %s", exc, exc_info=True)
        runtime.manager.mark_failure(str(exc))
        await core.safe_edit(status_msg, f"❌ <b>Ошибка:</b> {escape(str(exc))}")
    finally:
        session.active_provider = ""
        runtime.manager.set_final_result_callback(None)
        runtime.parser.clear_full_buffer()
