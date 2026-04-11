"""
BotCore — shared state and helpers for the Telegram bot.

Replaces the giant closure inside create_bot_and_setup() with a proper class.
All handler modules receive a BotCore instance and call its methods.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from core.config import settings
from core.orchestrator import OrchestrationPlan
from core.providers import (
    get_provider_definition,
    is_supported_provider,
    list_supported_provider_names,
    normalize_provider_name,
)
from core.provider_status_http import StatusHttpServer
from core.rate_limiter import RateLimiter
from runtime import RuntimeContainer
from core.security_audit import validate_prompt
from core.task_models import ChatSession, ProviderRuntime, QueuedTask, SubtaskRun, TaskResult, TaskRun

from bot.formatting import send_or_edit_structured
from bot.streaming import TelegramStreamRenderer

if TYPE_CHECKING:
    from core.process_manager import ClaudeProcessManager, CodexProcessManager, QwenProcessManager
    from core.parser import LogParser
    from core.file_manager import FileManager

log = logging.getLogger(__name__)


class BotCore:
    """
    Central object that owns the bot, dispatcher, runtime container, and
    all per-session state.  Passed as the first argument to every handler.
    """

    def __init__(
        self,
        manager=None,
        parser=None,
        file_mgr=None,
    ):
        self.bot = Bot(
            token=settings.TELEGRAM_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)

        self.container = RuntimeContainer(
            settings=settings,
            manager=manager,
            parser=parser,
            file_mgr=file_mgr,
        )
        self.executor = self.container.execution_service
        self.orchestrator = self.container.orchestrator_service
        self.default_provider: str = self.container.default_provider
        self.provider_paths: dict = self.container.provider_paths
        self.session_store = self.container.session_store

        self.rate_limiter = RateLimiter(
            max_requests=max(1, settings.RATE_LIMIT_MAX_REQUESTS),
            window_seconds=max(1, settings.RATE_LIMIT_WINDOW_SECONDS),
        )

        # Active TelegramStreamRenderer instances keyed by chat_id.
        # Used so callback handlers can resolve pending interaction futures.
        self._active_renderers: dict[int, TelegramStreamRenderer] = {}

        self._status_server: StatusHttpServer | None = None

    # ── renderer registry ─────────────────────────────────────────────────────

    def set_active_renderer(self, chat_id: int, renderer: TelegramStreamRenderer) -> None:
        self._active_renderers[chat_id] = renderer

    def clear_active_renderer(self, chat_id: int) -> None:
        self._active_renderers.pop(chat_id, None)

    def get_active_renderer(self, chat_id: int) -> TelegramStreamRenderer | None:
        return self._active_renderers.get(chat_id)

    # ── provider helpers ──────────────────────────────────────────────────────

    def provider_label(self, name: str) -> str:
        return get_provider_definition(name).label

    # ── session helpers ───────────────────────────────────────────────────────

    def get_session(self, chat_id: int) -> ChatSession:
        return self.container.get_session(chat_id)

    def get_runtime(self, session: ChatSession, provider_name: str) -> ProviderRuntime:
        return self.container.get_runtime(session, provider_name)

    async def ensure_runtime_started(self, session: ChatSession, provider_name: str) -> None:
        runtime = self.get_runtime(session, provider_name)
        if not runtime.manager.is_running:
            await runtime.manager.start()
        await self._ensure_worker_started(session)

    def remember_task_result(self, session: ChatSession, result: TaskResult) -> None:
        self.container.remember_task_result(session, result)

    # ── queue helpers ─────────────────────────────────────────────────────────

    def queued_count(self, session: ChatSession) -> int:
        return session.task_queue.qsize() + (1 if session.task_lock.locked() else 0)

    def queue_position(self, session: ChatSession, task: QueuedTask) -> int:
        try:
            items = list(session.task_queue._queue)
        except AttributeError:
            return 1
        try:
            return items.index(task) + 1
        except ValueError:
            return 1

    def clear_pending_queue(self, session: ChatSession) -> int:
        cleared = 0
        while True:
            try:
                session.task_queue.get_nowait()
                session.task_queue.task_done()
                cleared += 1
            except asyncio.QueueEmpty:
                break
        return cleared

    # ── message helpers ───────────────────────────────────────────────────────

    async def safe_edit(self, message: Message, text: str, **kwargs) -> bool:
        try:
            await message.edit_text(text, **kwargs)
            return True
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return False
            raise

    async def send_structured(self, message: Message, sections: list[str]) -> None:
        status = await message.answer("⏳ <b>Подготавливаю ответ…</b>")
        await send_or_edit_structured(self.bot, message, status, sections)

    # ── git ───────────────────────────────────────────────────────────────────

    async def run_git(self, work_dir: Path, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
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

    # ── access / rate-limit guards ────────────────────────────────────────────

    async def check_access(self, message: Message) -> bool:
        if str(message.from_user.id) not in [str(uid) for uid in settings.ALLOWED_USER_IDS]:
            await message.answer("⛔ Доступ запрещён.")
            return False
        return True

    async def guard_prompt(self, message: Message, prompt: str) -> bool:
        validation = validate_prompt(prompt, max_length=settings.MAX_PROMPT_LENGTH)
        if not validation.allowed:
            await message.answer(
                "⛔ <b>Запрос отклонён политикой безопасности.</b>\n"
                f"Причина: {escape(validation.reason)}"
            )
            return False
        user_id = str(message.from_user.id) if message.from_user else str(message.chat.id)
        ok, retry_after = self.rate_limiter.check(user_id)
        if not ok:
            await message.answer(
                "⏱️ <b>Слишком много запросов.</b>\n"
                f"Попробуйте снова примерно через <code>{retry_after}s</code>."
            )
            return False
        return True

    # ── task execution ────────────────────────────────────────────────────────

    async def execute_task(
        self,
        session: ChatSession,
        provider_name: str,
        prompt: str,
        status_msg: Message,
        status_prefix: str | None = None,
    ) -> TaskResult:
        """
        Run a single provider task with live streaming into status_msg.
        This is the core improvement over the old bot: we now attach a
        TelegramStreamRenderer so the user sees tool calls and thinking
        as they happen, not just a static "Выполняю…" message.
        """
        runtime = self.get_runtime(session, provider_name)
        renderer = TelegramStreamRenderer(self, status_msg, provider_name, session)

        try:
            return await self.executor.execute_provider_task(
                session=session,
                runtime=runtime,
                provider_name=provider_name,
                prompt=prompt,
                # status_callback intentionally omitted — renderer owns all edits
                stream_event_callback=renderer.on_stream_event,
                interaction_callback=renderer.on_interaction,
            )
        finally:
            await renderer.finalize()

    async def enqueue_task(
        self,
        session: ChatSession,
        provider_name: str,
        prompt: str,
        message: Message,
        queued_text: str,
        mode: str = "single",
        plan: OrchestrationPlan | None = None,
        resume_from: int = 0,
        prior_subtasks: list[SubtaskRun] | None = None,
    ) -> None:
        await self.ensure_runtime_started(session, provider_name)
        position = self.queued_count(session) + 1
        status_text = queued_text if position == 1 else self._queued_status_text(provider_name, position)
        from aiogram.types import InlineKeyboardMarkup
        reply_markup = None if mode == "orchestrated" else self._task_provider_keyboard(provider_name)
        status_msg = await message.answer(status_text, reply_markup=reply_markup)
        task = QueuedTask(
            provider=provider_name,
            prompt=prompt,
            anchor_message=message,
            status_message=status_msg,
            mode=mode,
            plan=plan,
            resume_from=resume_from,
            prior_subtasks=list(prior_subtasks or []),
        )
        session.pending_tasks[status_msg.message_id] = task
        await session.task_queue.put(task)

    def _queued_status_text(self, provider: str, pos: int) -> str:
        if pos <= 1:
            return f"⏳ <b>Запускаю {self.provider_label(provider)}…</b>"
        return (
            "⏳ <b>Задача поставлена в очередь.</b>\n"
            f"Провайдер: <b>{escape(provider)}</b>\n"
            f"Позиция: {pos}"
        )

    def _task_provider_keyboard(self, provider_name: str):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        buttons = [
            InlineKeyboardButton(
                text=f"✅ {self.provider_label(n)}" if provider_name == n else self.provider_label(n),
                callback_data=f"task_provider:{n}",
            )
            for n in list_supported_provider_names()
        ]
        return InlineKeyboardMarkup(inline_keyboard=[buttons])

    def provider_keyboard(self, session: ChatSession):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        current = session.current_provider
        buttons = [
            InlineKeyboardButton(
                text=f"✅ {self.provider_label(n)}" if current == n else self.provider_label(n),
                callback_data=f"set_provider:{n}",
            )
            for n in list_supported_provider_names()
        ]
        return InlineKeyboardMarkup(inline_keyboard=[buttons])

    # ── session worker ────────────────────────────────────────────────────────

    async def _ensure_worker_started(self, session: ChatSession) -> None:
        if session.worker_task is None or session.worker_task.done():
            session.worker_task = asyncio.create_task(self._session_worker(session))

    async def _session_worker(self, session: ChatSession) -> None:
        from bot.handlers.tasks import run_task, run_orchestrated_task

        while True:
            task = await session.task_queue.get()
            try:
                await self.ensure_runtime_started(session, task.provider)
                session.active_provider = task.provider
                async with session.task_lock:
                    task.started = True
                    if task.mode == "orchestrated" and task.plan is not None:
                        await self.safe_edit(task.status_message, "⏳ <b>Запускаю orchestrator…</b>")
                        await run_orchestrated_task(self, session, task.plan, task.anchor_message, task.status_message, task.resume_from, task.prior_subtasks)
                    else:
                        await self.safe_edit(
                            task.status_message,
                            f"⏳ <b>Запускаю {self.provider_label(task.provider)}…</b>",
                            reply_markup=self._task_provider_keyboard(task.provider),
                        )
                        await run_task(self, session, task.provider, task.prompt, task.anchor_message, task.status_message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Ошибка воркера сессии %s: %s", session.chat_id, exc, exc_info=True)
                try:
                    await self.safe_edit(
                        task.status_message,
                        f"❌ <b>Ошибка обработки задачи:</b> {escape(str(exc))}"
                    )
                except Exception:
                    pass
            finally:
                session.active_provider = ""
                session.pending_tasks.pop(task.status_message.message_id, None)
                session.task_queue.task_done()

    # ── HTTP status server ────────────────────────────────────────────────────

    def start_status_server(self) -> None:
        if not settings.ENABLE_STATUS_HTTP:
            return
        try:
            self._status_server = StatusHttpServer(
                host=settings.STATUS_HTTP_HOST,
                port=settings.STATUS_HTTP_PORT,
                health_provider=self._render_health,
                metrics_provider=self._render_metrics,
            )
            self._status_server.start()
        except OSError as exc:
            log.warning("Не удалось запустить status HTTP server: %s", exc)

    def _render_health(self) -> str:
        lines = ["provider health"]
        if not self.container.sessions:
            return "provider health\nno active sessions\n"
        for chat_id in sorted(self.container.sessions):
            session = self.container.sessions[chat_id]
            lines.append(f"chat {chat_id}:")
            for name in list_supported_provider_names():
                runtime = self.get_runtime(session, name)
                lines.extend(f"  {l}" for l in runtime.health.summary_lines())
        return "\n".join(lines) + "\n"

    def _render_metrics(self) -> str:
        lines = ["provider health"]
        if not self.container.sessions:
            lines.append("no active sessions")
            return self.container.metrics.render_prometheus(lines)
        for chat_id in sorted(self.container.sessions):
            session = self.container.sessions[chat_id]
            lines.append(f"chat {chat_id}:")
            for name in list_supported_provider_names():
                runtime = self.get_runtime(session, name)
                lines.extend(f"  {l}" for l in runtime.health.summary_lines())
        return self.container.metrics.render_prometheus(lines)
