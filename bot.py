import asyncio
import logging
from html import escape
from pathlib import Path
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, FSInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from orchestrator import OrchestrationPlan
from process_manager import ClaudeProcessManager, CodexProcessManager, QwenProcessManager
from parser import LogParser
from file_manager import FileManager
from config import settings
from providers import (
    get_provider_definition,
    is_supported_provider,
    list_supported_provider_names,
    normalize_provider_name,
    supported_provider_commands_text,
)
from runtime import RuntimeContainer
from task_models import ChatSession, ProviderRuntime, QueuedTask, SubtaskRun, TaskResult, TaskRun, utc_now_iso
from telegram_ui import (
    build_file_preview_messages,
    build_task_buttons,
    chunk_code_sections,
    format_status_message,
    format_task_result_sections,
    send_answer_chunks,
    send_or_edit_structured_message,
)

log = logging.getLogger(__name__)


def create_bot_and_setup(
    manager: QwenProcessManager | CodexProcessManager | ClaudeProcessManager | None = None,
    parser: LogParser | None = None,
    file_mgr: FileManager | None = None,
):
    router = Router()
    bot = Bot(
        token=settings.TELEGRAM_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    runtime_container = RuntimeContainer(
        settings=settings,
        manager=manager,
        parser=parser,
        file_mgr=file_mgr,
    )
    executor_service = runtime_container.execution_service
    orchestrator_service = runtime_container.orchestrator_service
    default_provider = runtime_container.default_provider
    provider_paths = runtime_container.provider_paths
    session_store = runtime_container.session_store

    def _provider_label(provider_name: str) -> str:
        return get_provider_definition(provider_name).label

    def _provider_keyboard(session: ChatSession) -> InlineKeyboardMarkup:
        current = session.current_provider
        buttons = [
            InlineKeyboardButton(
                text=f"✅ {get_provider_definition(name).label}" if current == name else get_provider_definition(name).label,
                callback_data=f"set_provider:{name}",
            )
            for name in list_supported_provider_names()
        ]
        return InlineKeyboardMarkup(inline_keyboard=[buttons])

    def _task_provider_keyboard(provider_name: str) -> InlineKeyboardMarkup:
        buttons = [
            InlineKeyboardButton(
                text=f"✅ {get_provider_definition(name).label}" if provider_name == name else get_provider_definition(name).label,
                callback_data=f"task_provider:{name}",
            )
            for name in list_supported_provider_names()
        ]
        return InlineKeyboardMarkup(inline_keyboard=[buttons])

    def _queued_status_text(provider_name: str, position: int) -> str:
        if position <= 1:
            return f"⏳ <b>Запускаю {_provider_label(provider_name)}…</b>"
        return (
            "⏳ <b>Задача поставлена в очередь.</b>\n"
            f"Провайдер: <b>{escape(provider_name)}</b>\n"
            f"Позиция: {position}"
        )

    def _get_session(chat_id: int) -> ChatSession:
        session = runtime_container.get_session(chat_id)
        log.info("Активна сессия для чата %s", chat_id)
        return session

    def _get_runtime(session: ChatSession, provider_name: str) -> ProviderRuntime:
        return runtime_container.get_runtime(session, provider_name)

    async def _ensure_worker_started(session: ChatSession):
        if session.worker_task is None or session.worker_task.done():
            session.worker_task = asyncio.create_task(_session_worker(session))

    async def _ensure_session_started(session: ChatSession, provider_name: str):
        runtime = _get_runtime(session, provider_name)
        if not runtime.manager.is_running:
            await runtime.manager.start()
        await _ensure_worker_started(session)

    def _remember_task_result(session: ChatSession, task_result: TaskResult):
        runtime_container.remember_task_result(session, task_result)

    def _queued_tasks_count(session: ChatSession) -> int:
        return session.task_queue.qsize() + (1 if session.task_lock.locked() else 0)

    def _queued_task_position(session: ChatSession, queued_task: QueuedTask) -> int:
        try:
            queue_items = list(session.task_queue._queue)
        except AttributeError:
            return 1
        try:
            return queue_items.index(queued_task) + 1
        except ValueError:
            return 1

    def _clear_pending_queue(session: ChatSession) -> int:
        cleared = 0
        while True:
            try:
                session.task_queue.get_nowait()
                session.task_queue.task_done()
                cleared += 1
            except asyncio.QueueEmpty:
                break
        return cleared

    async def _safe_edit_text(message: Message, text: str, **kwargs) -> bool:
        try:
            await message.edit_text(text, **kwargs)
            return True
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return False
            raise

    async def _run_git_command(work_dir: Path, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def _send_structured_reply(message: Message, sections: list[str]):
        status_msg = await message.answer("⏳ <b>Подготавливаю ответ…</b>")
        await send_or_edit_structured_message(bot, message, status_msg, sections)

    async def _send_provider_panel(session: ChatSession, message: Message):
        queue_info = _queued_tasks_count(session)
        provider_commands = ", ".join(
            f"<code>/provider {name}</code>" for name in list_supported_provider_names()
        )
        await message.answer(
            f"🤖 Провайдер по умолчанию: <b>{escape(session.current_provider)}</b>\n"
            f"▶️ Активный провайдер: <b>{escape(session.active_provider or session.current_provider)}</b>\n"
            f"🕘 В очереди: {queue_info}\n\n"
            "Можно переключить провайдера кнопками ниже или командами "
            f"{provider_commands}.",
            reply_markup=_provider_keyboard(session),
        )

    def _provider_status_lines(session: ChatSession) -> list[str]:
        lines = ["<b>📡 Provider health</b>"]
        for provider_name in list_supported_provider_names():
            runtime = _get_runtime(session, provider_name)
            lines.extend(runtime.health.summary_lines())
            lines.append("")
        return lines[:-1]

    async def _send_limits_view(session: ChatSession, message: Message):
        sections = ["<b>⏱️ Лимиты и доступность</b>"]
        sections.extend(_provider_status_lines(session)[1:])
        sections.append(
            "<i>Если CLI не умеет отдавать точные quota-данные, бот показывает последнюю известную причину отказа или limit event.</i>"
        )
        await _send_structured_reply(message, sections)

    async def _send_orchestration_plan(session: ChatSession, message: Message, prompt: str):
        planner = runtime_container.build_planner(session)
        plan = planner.build_plan(prompt)
        session.last_plan = plan
        session_store.save(session)
        sections = [
            "<b>🧭 План оркестрации</b>",
            f"<code>{escape(plan.prompt)}</code>",
            (
                f"<b>Сложность:</b> <code>{escape(plan.complexity)}</code>\n"
                f"<b>Стратегия:</b> {escape(plan.strategy)}"
            ),
        ]
        for index, subtask in enumerate(plan.subtasks, start=1):
            depends_on = ", ".join(subtask.depends_on) if subtask.depends_on else "none"
            sections.append(
                (
                    f"<b>{index}. {escape(subtask.title)}</b>\n"
                    f"<b>Тип:</b> <code>{escape(subtask.task_kind)}</code>\n"
                    f"<b>Агент:</b> <code>{escape(subtask.suggested_provider)}</code>\n"
                    f"<b>Depends on:</b> <code>{escape(depends_on)}</code>\n"
                    f"{escape(subtask.description)}\n"
                    f"<i>{escape(subtask.reason)}</i>"
                )
            )
        await _send_structured_reply(message, sections)

    def _history_lines(session: ChatSession, limit: int = 5) -> list[str]:
        lines = ["<b>🕘 Последние задачи</b>"]
        history_items = session.run_history or [TaskRun.from_task_result(item) for item in session.history]
        for index, item in enumerate(reversed(history_items[-limit:]), start=1):
            timestamp = escape(item.finished_or_started_at.replace("T", " ")[:19])
            prompt_preview = escape(item.prompt[:120] + ("…" if len(item.prompt) > 120 else ""))
            lines.append(
                f"{index}. {item.status_emoji} <code>{prompt_preview}</code>\n"
                f"   <i>{timestamp}</i> • {item.duration_text} • subtasks: {len(item.subtasks)} • файлов: {len(item.touched_files)}"
            )
        return lines

    def _runs_lines(session: ChatSession, limit: int = 10) -> list[str]:
        lines = ["<b>🏃 Последние run-ы</b>"]
        for index, item in enumerate(reversed(session.run_history[-limit:]), start=1):
            timestamp = escape(item.finished_or_started_at.replace("T", " ")[:19])
            lines.append(
                f"{index}. {item.status_emoji} <code>{escape(item.run_id)}</code>\n"
                f"   <i>{timestamp}</i> • {item.duration_text} • mode: <code>{escape(item.mode)}</code>"
            )
        return lines

    async def _send_artifact_detail(session: ChatSession, message: Message, entry_index: int):
        recent = list(reversed(session.run_history))
        if entry_index < 1 or entry_index > len(recent):
            await message.answer("❌ Нет run-а с таким номером.")
            return

        task_run = recent[entry_index - 1]
        artifact_path = Path(task_run.artifact_file) if task_run.artifact_file else None
        if not artifact_path or not artifact_path.exists():
            await message.answer("⚠️ Для этого run-а артефакт не найден.")
            return

        content = artifact_path.read_text(encoding="utf-8", errors="replace")
        active_runtime = _get_runtime(session, session.active_provider or session.current_provider)
        previews = build_file_preview_messages(artifact_path, content, active_runtime.parser._escape_html)
        for preview in previews[:3]:
            await message.answer(preview)

    async def _send_history_detail(session: ChatSession, message: Message, entry_index: int):
        source_history = session.run_history or [TaskRun.from_task_result(item) for item in session.history]
        recent = list(reversed(source_history))
        if entry_index < 1 or entry_index > len(recent):
            await message.answer("❌ Нет записи истории с таким номером.")
            return

        item = recent[entry_index - 1]
        sections = [
            f"<b>🕘 Задача #{entry_index}</b>",
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
                    (
                        f"• <code>{escape(subtask.subtask_id)}</code> — "
                        f"{escape(subtask.title)} "
                        f"(<b>{escape(subtask.provider)}</b>, {escape(subtask.status)})"
                    )
                    for subtask in item.subtasks
                )
            )
        if item.handoff_artifacts:
            sections.append(
                "<b>Handoff artifacts</b>\n"
                + "\n\n".join(f"<pre>{escape(artifact[:1200])}</pre>" for artifact in item.handoff_artifacts[:3])
            )
        sections.extend(
            format_task_result_sections(
                session.file_mgr.get_working_dir(),
                new_files=item.new_files or None,
                changed_files=item.changed_files or None,
            )
        )
        if item.error_text:
            sections.append(f"<b>❌ Ошибка</b>\n<pre>{escape(item.error_text[:3000])}</pre>")
        if item.review_answer:
            sections.append(f"<b>🔍 Review</b>\n<pre>{escape(item.review_answer[:3000])}</pre>")
        await _send_structured_reply(message, sections)
        await send_answer_chunks(
            bot,
            message,
            item.answer_text,
            _get_runtime(session, item.subtasks[0].provider if item.subtasks else session.current_provider).parser._escape_html,
            title="<b>📋 Ответ из истории</b>",
        )

    async def _enqueue_task(
        session: ChatSession,
        provider_name: str,
        prompt: str,
        message: Message,
        queued_text: str,
        mode: str = "single",
        plan: OrchestrationPlan | None = None,
        resume_from: int = 0,
        prior_subtasks: list[SubtaskRun] | None = None,
    ):
        await _ensure_session_started(session, provider_name)
        position = _queued_tasks_count(session) + 1
        status_text = queued_text if position == 1 else _queued_status_text(provider_name, position)
        reply_markup = None if mode == "orchestrated" else _task_provider_keyboard(provider_name)
        status_msg = await message.answer(status_text, reply_markup=reply_markup)
        queued_task = QueuedTask(
            provider=provider_name,
            prompt=prompt,
            anchor_message=message,
            status_message=status_msg,
            mode=mode,
            plan=plan,
            resume_from=resume_from,
            prior_subtasks=list(prior_subtasks or []),
        )
        session.pending_tasks[status_msg.message_id] = queued_task
        await session.task_queue.put(queued_task)

    async def _session_worker(session: ChatSession):
        while True:
            queued_task = await session.task_queue.get()
            try:
                await _ensure_session_started(session, queued_task.provider)
                runtime = _get_runtime(session, queued_task.provider)
                session.active_provider = queued_task.provider
                async with session.task_lock:
                    queued_task.started = True
                    if queued_task.mode == "orchestrated" and queued_task.plan is not None:
                        await _safe_edit_text(
                            queued_task.status_message,
                            "⏳ <b>Запускаю orchestrator…</b>",
                        )
                        await _run_orchestrated_task(
                            session,
                            queued_task.plan,
                            queued_task.anchor_message,
                            queued_task.status_message,
                            resume_from=queued_task.resume_from,
                            prior_subtasks=queued_task.prior_subtasks,
                        )
                    else:
                        await _safe_edit_text(
                            queued_task.status_message,
                            f"⏳ <b>Запускаю {_provider_label(queued_task.provider)}…</b>",
                            reply_markup=_task_provider_keyboard(queued_task.provider),
                        )
                        await _run_task(
                            session,
                            runtime,
                            queued_task.provider,
                            queued_task.prompt,
                            queued_task.anchor_message,
                            queued_task.status_message,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Ошибка воркера сессии %s: %s", session.chat_id, exc, exc_info=True)
                try:
                    await _safe_edit_text(
                        queued_task.status_message,
                        f"❌ <b>Ошибка обработки задачи:</b> {escape(str(exc))}"
                    )
                except Exception:
                    pass
            finally:
                session.active_provider = ""
                session.pending_tasks.pop(queued_task.status_message.message_id, None)
                session.task_queue.task_done()

    async def _send_diff(session: ChatSession, message: Message, mode: str = "last"):
        work_dir = session.file_mgr.get_working_dir()
        rc, _, _ = await _run_git_command(work_dir, "rev-parse", "--is-inside-work-tree")
        if rc != 0:
            await message.answer("⚠️ Текущая директория не является git-репозиторием.")
            return

        diff_files = session.last_task_result.touched_files if mode == "last" else []
        diff_args = ["diff", "--stat"]
        if diff_files:
            diff_args.extend(["--", *diff_files])

        rc, stat_stdout, stat_stderr = await _run_git_command(work_dir, *diff_args)
        if rc != 0:
            await message.answer(f"❌ Не удалось получить diff stat:\n<pre>{escape(stat_stderr[:1500])}</pre>")
            return

        patch_args = ["diff", "--", *diff_files] if diff_files else ["diff"]
        rc, diff_stdout, diff_stderr = await _run_git_command(work_dir, *patch_args)
        if rc != 0:
            await message.answer(f"❌ Не удалось получить diff:\n<pre>{escape(diff_stderr[:1500])}</pre>")
            return

        if not diff_stdout.strip():
            await message.answer("🟢 Изменений для показа нет.")
            return

        sections = ["<b>🧾 Diff последних изменений</b>"]
        if session.last_task_result.prompt:
            sections.append(f"<code>{escape(session.last_task_result.prompt[:160])}</code>")
        if stat_stdout.strip():
            sections.append(f"<pre>{escape(stat_stdout[:3000])}</pre>")
        await _send_structured_reply(message, sections)

        if mode == "stat":
            return

        diff_runtime = _get_runtime(session, session.last_task_result.provider or session.current_provider)
        diff_chunks = chunk_code_sections(diff_stdout, diff_runtime.parser._escape_html, language="diff", max_len=2800)
        for idx, chunk in enumerate(diff_chunks):
            title = "<b>🧾 Patch</b>" if idx == 0 else "<b>🧾 Patch</b> <i>(продолжение)</i>"
            await message.answer(f"{title}\n\n{chunk}")

    async def _run_task(
        session: ChatSession,
        runtime: ProviderRuntime,
        provider_name: str,
        prompt: str,
        message: Message,
        status_msg: Message,
    ):
        task_result = await _execute_provider_task(
            session=session,
            runtime=runtime,
            provider_name=provider_name,
            prompt=prompt,
            status_msg=status_msg,
        )
        session.last_task_result = task_result

        if task_result.exit_code == 0:
            answer_chunks = (
                chunk_code_sections(task_result.answer_text, runtime.parser._escape_html)
                if task_result.answer_text and task_result.answer_text.strip()
                else []
            )
            result_sections = format_task_result_sections(
                session.file_mgr.get_working_dir(),
                new_files=task_result.new_files if task_result.new_files else None,
                changed_files=task_result.changed_files if task_result.changed_files else None,
            )
            if answer_chunks:
                result_sections.extend(["<b>📋 Ответ агента</b>", answer_chunks[0]])
            _remember_task_result(session, task_result)
            keyboard = build_task_buttons(session.file_mgr.get_working_dir(), task_result.new_files, task_result.changed_files)

            if result_sections:
                await send_or_edit_structured_message(
                    bot,
                    message,
                    status_msg,
                    result_sections,
                    reply_markup=keyboard,
                )
            else:
                await _safe_edit_text(
                    status_msg,
                    "✅ <b>Задача выполнена.</b>",
                    reply_markup=keyboard,
                )

            await send_answer_chunks(
                bot,
                message,
                task_result.answer_text,
                runtime.parser._escape_html,
                skip_first_chunk=bool(answer_chunks),
            )
            return

        failure = runtime.manager.health.last_failure
        failure_lines = [f"⚠️ <b>{_provider_label(provider_name)} завершился с кодом {task_result.exit_code}</b>"]
        if failure:
            failure_lines.append(f"<b>Причина:</b> <code>{escape(failure.short_label)}</code>")
            failure_lines.append(escape(failure.message))
            if failure.retry_at:
                failure_lines.append(f"<b>Доступность:</b> примерно после <code>{escape(failure.retry_at)}</code>")
        _remember_task_result(session, task_result)
        await send_or_edit_structured_message(
            bot,
            message,
            status_msg,
            failure_lines,
        )

    async def _execute_provider_task(
        session: ChatSession,
        runtime: ProviderRuntime,
        provider_name: str,
        prompt: str,
        status_msg: Message,
        status_prefix: str | None = None,
    ) -> TaskResult:
        return await executor_service.execute_provider_task(
            session=session,
            runtime=runtime,
            provider_name=provider_name,
            prompt=prompt,
            status_callback=lambda text: _safe_edit_text(status_msg, text),
            status_prefix=status_prefix,
            status_formatter=format_status_message,
        )

    def _find_retry_start_index(task_run: TaskRun) -> int | None:
        return orchestrator_service.find_retry_start_index(task_run)

    async def _run_orchestrated_task(
        session: ChatSession,
        plan: OrchestrationPlan,
        message: Message,
        status_msg: Message,
        resume_from: int = 0,
        prior_subtasks: list[SubtaskRun] | None = None,
    ):
        task_run, _ = await orchestrator_service.run_orchestrated_task(
            session=session,
            plan=plan,
            status_callback=lambda text: _safe_edit_text(status_msg, text),
            resume_from=resume_from,
            prior_subtasks=prior_subtasks,
        )

        sections = [
            "<b>🧭 Оркестрация завершена</b>" if task_run.status == "success" else "<b>⚠️ Оркестрация завершена с ошибками</b>",
            f"<code>{escape(plan.prompt)}</code>",
            (
                f"<b>Сложность:</b> <code>{escape(plan.complexity)}</code>\n"
                f"<b>Стратегия:</b> {escape(plan.strategy)}\n"
                f"<b>Статус:</b> <code>{escape(task_run.status)}</code>\n"
                f"<b>Synthesis:</b> <code>{escape(task_run.synthesis_provider or '-')}</code>\n"
                f"<b>Review:</b> <code>{escape(task_run.review_provider or '-')}</code>"
            ),
            "<b>Subtasks</b>\n" + "\n".join(
                f"• <code>{escape(subtask.subtask_id)}</code> — {escape(subtask.title)} "
                f"(<b>{escape(subtask.provider)}</b>, {escape(subtask.status)})"
                for subtask in task_run.subtasks
            ),
        ]
        if task_run.handoff_artifacts:
            sections.append(
                "<b>Handoff artifacts</b>\n"
                + "\n\n".join(f"<pre>{escape(item[:1200])}</pre>" for item in task_run.handoff_artifacts[:3])
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
            can_retry_failed=task_run.mode == "orchestrated" and task_run.status in {"failed", "partial"},
        )
        await send_or_edit_structured_message(bot, message, status_msg, sections, reply_markup=keyboard)
        if task_run.answer_text.strip():
            runtime = _get_runtime(session, last_provider)
            await send_answer_chunks(
                bot,
                message,
                task_run.answer_text,
                runtime.parser._escape_html,
            )

    async def _handle_file_view(session: ChatSession, callback_query: CallbackQuery, file_path: str):
        fp = Path(file_path)
        if not fp.exists() or not fp.is_file():
            await callback_query.answer("Файл не найден", show_alert=True)
            return

        if fp.stat().st_size > 50_000:
            doc = FSInputFile(fp, filename=fp.name)
            await callback_query.message.answer_document(doc)
            await callback_query.answer()
        else:
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                runtime = _get_runtime(session, session.active_provider or session.current_provider)
                for preview in build_file_preview_messages(fp, content, runtime.parser._escape_html):
                    await callback_query.message.answer(preview)
                await callback_query.answer()
            except Exception as e:
                await callback_query.answer(f"Ошибка: {e}", show_alert=True)

    # --- Access check ---
    async def check_access(message: Message):
        if str(message.from_user.id) not in [str(uid) for uid in settings.ALLOWED_USER_IDS]:
            await message.answer("⛔ Доступ запрещён.")
            return False
        return True

    # --- Unified message handler ---
    @router.message()
    async def dispatch_message(message: Message):
        text = (message.text or "").strip()

        if not text:
            return

        # Access check for all except callback queries
        if not await check_access(message):
            return

        session = _get_session(message.chat.id)
        active_runtime = _get_runtime(session, session.active_provider or session.current_provider)

        if text == "/start":
            if any(runtime.manager.is_running for runtime in session.runtimes.values()):
                await message.answer("🤖 Агент уже работает. Отправляйте задачи!")
            else:
                await _ensure_session_started(session, session.current_provider)
                await message.answer(
                    f"🤖 <b>Multi-Agent Remote Control</b>\n\n"
                    f"Провайдер по умолчанию: <b>{escape(session.current_provider)}</b>\n\n"
                    "Отправьте текст — агент выполнит задачу.\n\n"
                    "Используйте /help для списка команд."
                )

        elif text == "/help":
            await message.answer(
                "📋 <b>Доступные команды:</b>\n\n"
                "<b>Файлы:</b>\n"
                "/ls [путь] — содержимое директории\n"
                "/cat &lt;файл&gt; — содержимое файла\n"
                "/tree [путь] — дерево файлов\n"
                "/cd &lt;путь&gt; — сменить директорию\n"
                "/pwd — текущая директория\n\n"
                "<b>Проекты:</b>\n"
                "/project &lt;имя&gt; &lt;путь&gt; — сохранить проект\n"
                "/load &lt;имя&gt; — загрузить проект\n"
                "/projects — список проектов\n\n"
                "<b>Управление:</b>\n"
                "/provider — текущий провайдер\n"
                "/agents — кнопки выбора провайдера\n"
                f"/provider {supported_provider_commands_text()} — сменить провайдера по умолчанию\n"
                "/qwen &lt;задача&gt; — разовая задача через qwen\n"
                "/codex &lt;задача&gt; — разовая задача через codex\n"
                "/claude &lt;задача&gt; — разовая задача через claude\n"
                "<i>Под статусом задачи тоже можно переключить провайдера кнопками.</i>\n"
                "/status — статус и прогресс\n"
                "/limits — лимиты и доступность агентов\n"
                "/history — последние задачи\n"
                "/runs — последние run-ы\n"
                "/artifacts 1 — показать артефакт run-а\n"
                "/diff — diff последних изменений\n"
                "/plan &lt;задача&gt; — черновой план оркестрации\n"
                "/orchestrate &lt;задача&gt; — выполнить план по подзадачам\n"
                "/retry_failed — продолжить последнюю orchestration-задачу с места сбоя\n"
                "/cancel — отмена текущей задачи\n"
                "/btw &lt;вопрос&gt; — вопрос агенту\n"
                "/clear — сбросить сессию\n"
                "/help — эта справка"
            )

        elif text == "/ls":
            await message.answer(session.file_mgr.list_dir(None))
        elif text.startswith("/ls "):
            await message.answer(session.file_mgr.list_dir(text[3:].strip()))

        elif text == "/pwd":
            await message.answer(f"📂 <code>{session.file_mgr.get_working_dir()}</code>")

        elif text == "/cancel":
            for runtime in session.runtimes.values():
                await runtime.manager.stop()
                runtime.parser.clear_full_buffer()
            dropped = _clear_pending_queue(session)
            extra = f" Очищено из очереди: {dropped}." if dropped else ""
            await message.answer(f"🛑 Задача отменена.{extra}")

        elif text == "/clear":
            for runtime in session.runtimes.values():
                await runtime.manager.stop()
                runtime.parser.clear_full_buffer()
            session.last_task_result = TaskResult(provider=session.current_provider)
            session.last_task_run = None
            dropped = _clear_pending_queue(session)
            session.history.clear()
            session.run_history.clear()
            session.last_plan = None
            session_store.clear(session.chat_id)
            extra = f" Из очереди удалено: {dropped}." if dropped else ""
            await message.answer(f"🗑 Сессия сброшена.{extra}")

        elif text == "/provider":
            await _send_provider_panel(session, message)
        elif text == "/agents":
            await _send_provider_panel(session, message)
        elif text.startswith("/provider "):
            requested = text.split(None, 1)[1].strip().lower()
            if not is_supported_provider(requested):
                providers_text = ", ".join(f"<code>{name}</code>" for name in list_supported_provider_names())
                await message.answer(f"❌ Доступные провайдеры: {providers_text}.")
            else:
                session.current_provider = normalize_provider_name(requested)
                session_store.save(session)
                await message.answer(
                    f"✅ Провайдер по умолчанию переключён на <b>{escape(requested)}</b>.",
                    reply_markup=_provider_keyboard(session),
                )

        elif text == "/status":
            queue_info = f"\n🕘 В очереди: {session.task_queue.qsize()}"
            provider_lines = "\n".join(_provider_status_lines(session))
            await message.answer(
                f"🤖 Провайдер: <b>{escape(session.active_provider or session.current_provider)}</b>\n"
                + active_runtime.parser.get_status_text()
                + queue_info
                + "\n\n"
                + provider_lines
                + "\n\n"
                + session.file_mgr.get_project_context()
            )
        elif text == "/limits":
            await _send_limits_view(session, message)

        elif text == "/history":
            if not session.run_history and not session.history:
                await message.answer("🕘 История задач пока пуста.")
            else:
                lines = _history_lines(session)
                lines.append("\n<i>Чтобы открыть запись: /history 1</i>")
                await message.answer("\n".join(lines))
        elif text == "/runs":
            if not session.run_history:
                await message.answer("🏃 Run-ов пока нет.")
            else:
                lines = _runs_lines(session)
                lines.append("\n<i>Чтобы открыть артефакт: /artifacts 1</i>")
                await message.answer("\n".join(lines))
        elif text.startswith("/artifacts "):
            arg = text.split(None, 1)[1].strip()
            if not arg.isdigit():
                await message.answer("📝 Использование: <code>/artifacts 1</code>")
            else:
                await _send_artifact_detail(session, message, int(arg))
        elif text == "/artifacts":
            await message.answer("📝 Использование: <code>/artifacts 1</code>")

        elif text.startswith("/history "):
            arg = text[9:].strip()
            if not arg.isdigit():
                await message.answer("📝 Использование: <code>/history</code> или <code>/history 1</code>")
            else:
                await _send_history_detail(session, message, int(arg))

        elif text == "/diff":
            await _send_diff(session, message, mode="last")
        elif text in ("/diff --stat", "/diff stat"):
            await _send_diff(session, message, mode="stat")
        elif text in ("/diff --full", "/diff full"):
            await _send_diff(session, message, mode="full")
        elif text.startswith("/plan "):
            await _send_orchestration_plan(session, message, text[6:].strip())
        elif text == "/plan":
            await message.answer("📝 Использование: <code>/plan &lt;задача&gt;</code>")
        elif text.startswith("/orchestrate "):
            plan_prompt = text[len("/orchestrate "):].strip()
            planner = runtime_container.build_planner(session)
            plan = planner.build_plan(plan_prompt)
            session.last_plan = plan
            session_store.save(session)
            await _enqueue_task(
                session,
                plan.subtasks[0].suggested_provider if plan.subtasks else session.current_provider,
                plan_prompt,
                message,
                "⏳ <b>Запускаю orchestrator…</b>",
                mode="orchestrated",
                plan=plan,
            )
        elif text == "/orchestrate":
            await message.answer("📝 Использование: <code>/orchestrate &lt;задача&gt;</code>")
        elif text == "/retry_failed":
            last_run = session.last_task_run
            if not last_run or last_run.mode != "orchestrated":
                await message.answer("⚠️ Последняя задача не была orchestration-run.")
            else:
                retry_index = _find_retry_start_index(last_run)
                if retry_index is None:
                    await message.answer("🟢 В последнем orchestration-run нет упавшей подзадачи.")
                else:
                    plan = session.last_plan or runtime_container.build_planner(session).build_plan(last_run.prompt)
                    session.last_plan = plan
                    session_store.save(session)
                    retry_provider = (
                        plan.subtasks[retry_index].suggested_provider
                        if retry_index < len(plan.subtasks)
                        else (last_run.synthesis_provider or session.current_provider)
                    )
                    await _enqueue_task(
                        session,
                        retry_provider,
                        last_run.prompt,
                        message,
                        f"⏳ <b>Возобновляю orchestrator с шага {retry_index + 1}…</b>",
                        mode="orchestrated",
                        plan=plan,
                        resume_from=retry_index,
                        prior_subtasks=last_run.subtasks,
                    )
        elif text.startswith("/qwen "):
            await _enqueue_task(session, "qwen", text[6:].strip(), message, "⏳ <b>Запускаю qwen…</b>")
        elif text == "/qwen":
            session.current_provider = "qwen"
            session_store.save(session)
            await message.answer(
                "✅ Провайдер по умолчанию переключён на <b>qwen</b>.",
                reply_markup=_provider_keyboard(session),
            )
        elif text.startswith("/codex "):
            await _enqueue_task(session, "codex", text[7:].strip(), message, "⏳ <b>Запускаю codex…</b>")
        elif text == "/codex":
            session.current_provider = "codex"
            session_store.save(session)
            await message.answer(
                "✅ Провайдер по умолчанию переключён на <b>codex</b>.",
                reply_markup=_provider_keyboard(session),
            )
        elif text.startswith("/claude "):
            await _enqueue_task(session, "claude", text[8:].strip(), message, "⏳ <b>Запускаю Claude…</b>")
        elif text == "/claude":
            session.current_provider = "claude"
            session_store.save(session)
            await message.answer(
                "✅ Провайдер по умолчанию переключён на <b>claude</b>.",
                reply_markup=_provider_keyboard(session),
            )

        elif text == "/projects":
            await message.answer(session.file_mgr.list_projects())

        elif text.startswith("/cd "):
            path = text[3:].strip()
            result = session.file_mgr.set_working_dir(path)
            await message.answer(result)

        elif text.startswith("/cat "):
            path = text[4:].strip()
            target = Path(path)
            if not target.is_absolute():
                target = session.file_mgr.get_working_dir() / target
            target = target.resolve()
            if err := session.file_mgr._check_path_safe(target):
                await message.answer(err)
            elif not target.exists():
                await message.answer(f"❌ Файл не найден: <code>{target.name}</code>")
            elif target.stat().st_size > 50_000:
                doc = FSInputFile(target, filename=target.name)
                await message.answer_document(doc)
            else:
                content = target.read_text(encoding="utf-8", errors="replace")
                for preview in build_file_preview_messages(target, content, active_runtime.parser._escape_html):
                    await message.answer(preview)

        elif text.startswith("/tree"):
            args = text.replace("/tree", "").strip()
            result = session.file_mgr.tree(args if args else None)
            for chunk in build_file_preview_messages(Path("tree.txt"), result, active_runtime.parser._escape_html):
                tree_text = chunk.replace("<b>tree.txt</b>\n\n", "", 1)
                await message.answer(tree_text)

        elif text.startswith("/project "):
            parts = text.split(None, 2)
            if len(parts) < 3:
                await message.answer("📝 Использование: <code>/project &lt;имя&gt; &lt;путь&gt;</code>")
            else:
                result = session.file_mgr.set_project(parts[1], parts[2])
                await message.answer(result)

        elif text.startswith("/load "):
            name = text[5:].strip()
            result = session.file_mgr.load_project(name)
            await message.answer(result)

        elif text.startswith("/btw "):
            if session.task_lock.locked() or not session.task_queue.empty():
                await message.answer("⏳ В этом чате уже есть активная или ожидающая задача.")
                return
            provider_name = session.current_provider
            runtime = _get_runtime(session, provider_name)
            await _ensure_session_started(session, provider_name)
            question = text[4:].strip()
            status_msg = await message.answer(f"❓ Спрашиваю: <i>{question}</i>")
            try:
                async with session.task_lock:
                    session.active_provider = provider_name
                    runtime.parser.clear_full_buffer()
                    runtime.parser.set_final_result("")
                    runtime.manager.set_final_result_callback(lambda text: runtime.parser.set_final_result(text))
                    await runtime.manager.send_command(question, cwd=session.file_mgr.get_working_dir())
                    response = runtime.parser.final_result
                if not response:
                    response = runtime.parser.get_full_response()
                if response and response.strip():
                    sections = [
                        f"<b>💬 Ответ ({escape(provider_name)})</b>",
                        *chunk_code_sections(response, runtime.parser._escape_html),
                    ]
                    await send_or_edit_structured_message(bot, message, status_msg, sections)
                else:
                    await _safe_edit_text(status_msg, "⚠️ <b>Не удалось получить ответ.</b>")
            except Exception as e:
                log.error(f"Ошибка /btw: {e}", exc_info=True)
                runtime.manager.mark_failure(str(e))
                await _safe_edit_text(status_msg, f"❌ <b>Ошибка:</b> {escape(str(e))}")
            finally:
                session.active_provider = ""
                runtime.manager.set_final_result_callback(None)
                runtime.parser.clear_full_buffer()

        elif text.startswith("/btw"):
            await message.answer("📝 Использование: <code>/btw Ваш вопрос</code>")

        else:
            provider_name = session.current_provider
            await _enqueue_task(
                session,
                provider_name,
                text,
                message,
                f"⏳ <b>Запускаю {_provider_label(provider_name)}…</b>",
            )

    # --- Callback queries (inline buttons) ---
    @router.callback_query()
    async def dispatch_callback(callback_query: CallbackQuery):
        data = callback_query.data or ""
        if not callback_query.message:
            await callback_query.answer()
            return
        session = _get_session(callback_query.message.chat.id)
        if data.startswith("view_file:"):
            file_path = data.split(":", 1)[1]
            await _handle_file_view(session, callback_query, file_path)
        elif data.startswith("task_provider:"):
            provider_name = data.split(":", 1)[1].strip().lower()
            if not is_supported_provider(provider_name):
                await callback_query.answer("Неизвестный провайдер", show_alert=True)
                return
            queued_task = session.pending_tasks.get(callback_query.message.message_id)
            if queued_task is None:
                await callback_query.answer("Эта задача уже завершена", show_alert=True)
                return
            if queued_task.mode == "orchestrated":
                await callback_query.answer("Для orchestrator-задачи провайдеры задаются планом", show_alert=True)
                return
            if queued_task.started:
                await callback_query.answer("Задача уже запущена", show_alert=True)
                return
            queued_task.provider = provider_name
            await callback_query.answer(f"Для задачи выбран {provider_name}")
            await _safe_edit_text(
                callback_query.message,
                _queued_status_text(provider_name, _queued_task_position(session, queued_task)),
                reply_markup=_task_provider_keyboard(provider_name),
            )
        elif data.startswith("set_provider:"):
            provider_name = data.split(":", 1)[1].strip().lower()
            if not is_supported_provider(provider_name):
                await callback_query.answer("Неизвестный провайдер", show_alert=True)
                return
            session.current_provider = normalize_provider_name(provider_name)
            session_store.save(session)
            await callback_query.answer(f"Провайдер переключён на {provider_name}")
            provider_commands = ", ".join(
                f"<code>/provider {name}</code>" for name in list_supported_provider_names()
            )
            await _safe_edit_text(
                callback_query.message,
                (
                    f"🤖 Провайдер по умолчанию: <b>{escape(session.current_provider)}</b>\n"
                    f"▶️ Активный провайдер: <b>{escape(session.active_provider or session.current_provider)}</b>\n"
                    f"🕘 В очереди: {_queued_tasks_count(session)}\n\n"
                    "Можно переключить провайдера кнопками ниже или командами "
                    f"{provider_commands}."
                ),
                reply_markup=_provider_keyboard(session),
            )
        elif data == "repeat_task":
            if session.last_task_run and session.last_task_run.mode == "orchestrated" and session.last_task_run.prompt:
                planner = runtime_container.build_planner(session)
                plan = planner.build_plan(session.last_task_run.prompt)
                session.last_plan = plan
                session_store.save(session)
                await callback_query.answer("План повторно добавлен в очередь")
                await _enqueue_task(
                    session,
                    plan.subtasks[0].suggested_provider if plan.subtasks else session.current_provider,
                    session.last_task_run.prompt,
                    callback_query.message,
                    "⏳ <b>Повторяю orchestration plan…</b>",
                    mode="orchestrated",
                    plan=plan,
                )
                return
            if not session.last_task_result.prompt:
                await callback_query.answer("Нет предыдущей задачи", show_alert=True)
                return
            await callback_query.answer("Задача добавлена в очередь")
            await _enqueue_task(
                session,
                session.last_task_result.provider or session.current_provider,
                session.last_task_result.prompt,
                callback_query.message,
                "⏳ <b>Повторяю последнюю задачу…</b>",
            )
        elif data == "retry_failed_subtask":
            last_run = session.last_task_run
            if not last_run or last_run.mode != "orchestrated":
                await callback_query.answer("Нет orchestration-задачи для retry", show_alert=True)
                return
            retry_index = _find_retry_start_index(last_run)
            if retry_index is None:
                await callback_query.answer("Упавших подзадач нет", show_alert=True)
                return
            plan = session.last_plan or runtime_container.build_planner(session).build_plan(last_run.prompt)
            session.last_plan = plan
            session_store.save(session)
            retry_provider = (
                plan.subtasks[retry_index].suggested_provider
                if retry_index < len(plan.subtasks)
                else (last_run.synthesis_provider or session.current_provider)
            )
            await callback_query.answer(f"Retry с шага {retry_index + 1}")
            await _enqueue_task(
                session,
                retry_provider,
                last_run.prompt,
                callback_query.message,
                f"⏳ <b>Возобновляю orchestrator с шага {retry_index + 1}…</b>",
                mode="orchestrated",
                plan=plan,
                resume_from=retry_index,
                prior_subtasks=last_run.subtasks,
            )
        elif data == "show_details":
            if not session.last_task_result.has_details:
                await callback_query.answer("Подробностей пока нет", show_alert=True)
                return
            await callback_query.answer()
            last_run = session.last_task_run or TaskRun.from_task_result(session.last_task_result)
            sections = [
                f"<b>📝 Последняя задача</b>\n<code>{escape(session.last_task_result.prompt)}</code>",
                f"<b>🤖 Провайдер:</b> <code>{escape(session.last_task_result.provider)}</code>",
            ]
            if last_run.strategy:
                sections.append(f"<b>Strategy:</b> {escape(last_run.strategy)}")
            if last_run.synthesis_provider:
                sections.append(f"<b>Synthesis:</b> <code>{escape(last_run.synthesis_provider)}</code>")
            if last_run.review_provider:
                sections.append(f"<b>Review:</b> <code>{escape(last_run.review_provider)}</code>")
            if last_run.subtasks:
                sections.append(
                    "<b>Subtasks</b>\n"
                    + "\n".join(
                        f"• <code>{escape(subtask.subtask_id)}</code> — {escape(subtask.title)} ({escape(subtask.provider)})"
                        for subtask in last_run.subtasks
                    )
                )
            if last_run.handoff_artifacts:
                sections.append(
                    "<b>Handoff artifacts</b>\n"
                    + "\n\n".join(f"<pre>{escape(artifact[:1200])}</pre>" for artifact in last_run.handoff_artifacts[:3])
                )
            if last_run.review_answer:
                sections.append(f"<b>🔍 Review</b>\n<pre>{escape(last_run.review_answer[:3000])}</pre>")
            sections.extend(
                format_task_result_sections(
                    session.file_mgr.get_working_dir(),
                    new_files=session.last_task_result.new_files or None,
                    changed_files=session.last_task_result.changed_files or None,
                )
            )
            if session.last_task_result.touched_files:
                sections.append(
                    "<b>📁 Последние файлы</b>\n" +
                    "\n".join(f"• <code>{escape(Path(path).name)}</code>" for path in session.last_task_result.touched_files[:10])
                )
            await send_or_edit_structured_message(
                bot,
                callback_query.message,
                callback_query.message,
                sections,
            )
            await send_answer_chunks(
                bot,
                callback_query.message,
                session.last_task_result.answer_text,
                _get_runtime(session, session.last_task_result.provider or session.current_provider).parser._escape_html,
            )
        else:
            await callback_query.answer()

    return bot, dp
