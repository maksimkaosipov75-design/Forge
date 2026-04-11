"""
Informational and session management commands:
/start, /help, /commands, /status, /limits, /usage, /metrics,
/cancel, /clear, /compact, /todos
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from providers import list_supported_provider_names, supported_provider_commands_text
from task_models import ChatSession, TaskResult, TaskRun

from bot.formatting import chunk_code_sections, send_or_edit_structured

if TYPE_CHECKING:
    from aiogram.types import Message
    from bot.core import BotCore


async def handle_start(core: "BotCore", message: "Message", session: ChatSession) -> None:
    if any(rt.manager.is_running for rt in session.runtimes.values()):
        await message.answer("🤖 Агент уже работает. Отправляйте задачи!")
    else:
        await core.ensure_runtime_started(session, session.current_provider)
        await message.answer(
            f"🤖 <b>Multi-Agent Remote Control</b>\n\n"
            f"Провайдер по умолчанию: <b>{escape(session.current_provider)}</b>\n\n"
            "Отправьте текст — агент выполнит задачу.\n\n"
            "Используйте /help для списка команд."
        )


async def handle_help(core: "BotCore", message: "Message") -> None:
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
        f"/provider {supported_provider_commands_text()} — сменить провайдера\n"
        "/qwen &lt;задача&gt;, /codex &lt;задача&gt;, /claude &lt;задача&gt;\n"
        "/status — статус и прогресс\n"
        "/limits — лимиты и доступность\n"
        "/metrics — внутренние метрики\n"
        "/history — последние задачи\n"
        "/runs — последние run-ы\n"
        "/artifacts 1 — артефакт run-а\n"
        "/diff — diff последних изменений\n"
        "/review [фокус] — code review последнего результата\n"
        "/commit [сообщение] — git commit\n"
        "/plan &lt;задача&gt; — план оркестрации\n"
        "/orchestrate &lt;задача&gt; — выполнить план\n"
        "/retry_failed — продолжить с места сбоя\n"
        "/recover — восстановить из чекпоинта (после краша)\n"
        "/cancel — отмена задачи\n"
        "/btw &lt;вопрос&gt; — вопрос агенту\n"
        "/clear — сбросить сессию\n"
        "/help — эта справка"
    )


async def handle_commands(core: "BotCore", message: "Message") -> None:
    await message.answer(
        "📚 <b>Команды</b>\n\n"
        "<b>Основные</b>\n/help, /commands, /status, /limits, /provider, /agents\n\n"
        "<b>Задачи</b>\n/plan, /orchestrate, /retry_failed, /recover, /btw, /qwen, /codex, /claude\n\n"
        "<b>История и артефакты</b>\n/history, /runs, /artifacts 1, /diff, /todos, /usage, /metrics, /review\n\n"
        "<b>Сессия</b>\n/clear, /cancel, /compact [N|фильтр]\n\n"
        "<b>Модели и провайдеры</b>\n/model, /model &lt;provider&gt;, /model &lt;provider&gt; &lt;model&gt;\n/reset-provider, /commit\n\n"
        "<b>Файлы</b>\n/pwd, /ls, /tree, /cat, /cd"
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
        f"🤖 Провайдер: <b>{escape(active)}</b>\n"
        + runtime.parser.get_status_text()
        + f"\n🕘 В очереди: {session.task_queue.qsize()}"
        + "\n\n"
        + provider_block
        + "\n\n"
        + session.file_mgr.get_project_context()
    )


async def handle_limits(core: "BotCore", message: "Message", session: ChatSession) -> None:
    sections = ["<b>⏱️ Лимиты и доступность</b>"]
    for name in list_supported_provider_names():
        rt = core.get_runtime(session, name)
        sections.extend(rt.health.summary_lines())
    sections.append(
        "<i>Если CLI не умеет отдавать точные quota-данные, показывается последняя известная причина отказа.</i>"
    )
    await core.send_structured(message, sections)


async def handle_usage(core: "BotCore", message: "Message", session: ChatSession) -> None:
    sections = ["<b>📊 Использование за сессию</b>"]
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
            f"Модель: <code>{escape(model)}</code>\n"
            f"Задач: {tasks} • успех: {success} • ошибки: {fail}\n"
            f"Последний usage: <code>{last_in}</code> in / <code>{last_out}</code> out\n"
            f"Суммарно: <code>{total_in}</code> in / <code>{total_out}</code> out"
        )
    sections.append(
        "<i>Показываются токены из stream. Если провайдер их не отдавал, значения нулевые.</i>"
    )
    await core.send_structured(message, sections)


async def handle_metrics(core: "BotCore", message: "Message") -> None:
    payload = core._render_metrics()
    sections = [
        "<b>📈 Metrics</b>",
        f"<pre>{escape(payload[:3500])}</pre>",
    ]
    if len(payload) > 3500:
        sections.append("<i>Вывод обрезан. Полная версия: HTTP endpoint /metrics.</i>")
    await core.send_structured(message, sections)


async def handle_todos(core: "BotCore", message: "Message", session: ChatSession) -> None:
    todos: list[str] = []
    for line in (session.last_task_result.answer_text or "").splitlines():
        s = line.strip()
        if s.startswith(("- [ ] ", "* [ ] ", "- [x] ", "* [x] ", "TODO:", "Todo:", "todo:")):
            todos.append(s)
    if not todos:
        await message.answer("📝 В последнем ответе TODO-список не найден.")
    else:
        await core.send_structured(message, ["<b>📝 TODO из последнего ответа</b>", *todos[:20]])


async def handle_cancel(core: "BotCore", message: "Message", session: ChatSession) -> None:
    for rt in session.runtimes.values():
        await rt.manager.stop()
        rt.parser.clear_full_buffer()
    dropped = core.clear_pending_queue(session)
    extra = f" Очищено из очереди: {dropped}." if dropped else ""
    await message.answer(f"🛑 Задача отменена.{extra}")


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
    extra = f" Из очереди удалено: {dropped}." if dropped else ""
    await message.answer(f"🗑 Сессия сброшена.{extra}")


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
        await message.answer(f"🗜 История сжата. Оставлено: <b>{keep}</b>.")
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
            f"🗜 История отфильтрована по <code>{escape(arg)}</code>. "
            f"Задач: {len(session.history)}, run-ов: {len(session.run_history)}."
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
        await message.answer(f"🗜 История сжата до последних <b>{keep}</b> записей.")
