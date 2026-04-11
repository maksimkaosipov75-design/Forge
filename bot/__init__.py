"""
bot package — entry point.

    from bot import create_bot_and_setup
    bot, dp = create_bot_and_setup()

Replaces the monolithic bot.py with a proper package.
"""

from __future__ import annotations

import logging
from html import escape
from typing import TYPE_CHECKING

from aiogram.types import CallbackQuery, Message

from providers import is_supported_provider, list_supported_provider_names, normalize_provider_name

from bot.core import BotCore
from bot.handlers.callbacks import dispatch_callback
from bot.handlers.commands import (
    handle_cancel,
    handle_clear,
    handle_commands,
    handle_compact,
    handle_help,
    handle_limits,
    handle_metrics,
    handle_start,
    handle_status,
    handle_todos,
    handle_usage,
)
from bot.handlers.files import (
    handle_cat,
    handle_cd,
    handle_ls,
    handle_project_load,
    handle_project_set,
    handle_projects,
    handle_pwd,
    handle_tree,
)
from bot.handlers.history import (
    handle_artifact,
    handle_diff,
    handle_history_detail,
    handle_history_list,
    handle_runs,
)
from bot.handlers.providers import (
    handle_model_overview,
    handle_model_set,
    handle_provider_panel,
    handle_reset_provider,
    handle_set_provider,
)
from bot.handlers.workflow import (
    handle_btw,
    handle_commit,
    handle_orchestrate,
    handle_plan,
    handle_recover,
    handle_retry_failed,
    handle_review,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def create_bot_and_setup(manager=None, parser=None, file_mgr=None):
    """
    Build a BotCore, register all message/callback handlers, start the HTTP
    status server and return (bot, dp) — the same contract as the old bot.py.
    """
    core = BotCore(manager=manager, parser=parser, file_mgr=file_mgr)

    # ── message handler ───────────────────────────────────────────────────────

    @core.router.message()
    async def _dispatch_message(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        if not await core.check_access(message):
            return

        session = core.get_session(message.chat.id)

        # ── informational / session management ────────────────────────────────
        if text == "/start":
            await handle_start(core, message, session)
            return

        if text in ("/help", "/help@bot"):
            await handle_help(core, message)
            return

        if text == "/commands":
            await handle_commands(core, message)
            return

        if text == "/status":
            await handle_status(core, message, session)
            return

        if text == "/limits":
            await handle_limits(core, message, session)
            return

        if text == "/usage":
            await handle_usage(core, message, session)
            return

        if text == "/metrics":
            await handle_metrics(core, message)
            return

        if text == "/todos":
            await handle_todos(core, message, session)
            return

        if text == "/cancel":
            await handle_cancel(core, message, session)
            return

        if text == "/clear":
            await handle_clear(core, message, session)
            return

        if text.startswith("/compact"):
            arg = text.split(None, 1)[1].strip() if " " in text else ""
            await handle_compact(core, message, session, arg)
            return

        # ── provider management ───────────────────────────────────────────────
        if text in ("/provider", "/agents"):
            await handle_provider_panel(core, message, session)
            return

        if text.startswith("/provider "):
            await handle_set_provider(core, message, session, text.split(None, 1)[1].strip().lower())
            return

        if text == "/reset-provider":
            await handle_reset_provider(core, message, session)
            return

        if text == "/model":
            await handle_model_overview(core, message, session)
            return

        if text.startswith("/model "):
            arg = text.split(None, 1)[1].strip()
            parts = arg.split(maxsplit=1)
            target = parts[0].lower()
            if target not in list_supported_provider_names():
                target = session.current_provider
                new_model = arg
            else:
                new_model = parts[1].strip() if len(parts) > 1 else ""
            if not new_model:
                await handle_model_overview(core, message, session, target_provider=target)
            else:
                await handle_model_set(core, message, session, target, new_model)
            return

        # ── per-provider direct commands ─────────────────────────────────────
        for pname in list_supported_provider_names():
            cmd = f"/{pname}"
            if text == cmd:
                session.current_provider = normalize_provider_name(pname)
                core.session_store.save(session)
                await message.answer(
                    f"✅ Провайдер по умолчанию переключён на <b>{escape(pname)}</b>.",
                    reply_markup=core.provider_keyboard(session),
                )
                return
            if text.startswith(cmd + " "):
                prompt = text[len(cmd):].strip()
                if not await core.guard_prompt(message, prompt):
                    return
                label = core.provider_label(pname)
                await core.enqueue_task(
                    session, pname, prompt, message,
                    f"⏳ <b>Запускаю {escape(label)}…</b>",
                )
                return

        # ── history / artifacts / diff ────────────────────────────────────────
        if text == "/history":
            await handle_history_list(core, message, session)
            return

        if text.startswith("/history "):
            arg = text[9:].strip()
            if not arg.isdigit():
                await message.answer("📝 Использование: <code>/history</code> или <code>/history 1</code>")
            else:
                await handle_history_detail(core, message, session, int(arg))
            return

        if text == "/runs":
            await handle_runs(core, message, session)
            return

        if text == "/artifacts":
            await message.answer("📝 Использование: <code>/artifacts 1</code>")
            return

        if text.startswith("/artifacts "):
            arg = text.split(None, 1)[1].strip()
            if not arg.isdigit():
                await message.answer("📝 Использование: <code>/artifacts 1</code>")
            else:
                await handle_artifact(core, message, session, int(arg))
            return

        if text in ("/diff", "/diff --full", "/diff full"):
            mode = "last" if text == "/diff" else "full"
            await handle_diff(core, message, session, mode)
            return

        if text in ("/diff --stat", "/diff stat"):
            await handle_diff(core, message, session, mode="stat")
            return

        # ── filesystem ────────────────────────────────────────────────────────
        if text == "/pwd":
            await handle_pwd(core, message, session)
            return

        if text == "/ls":
            await handle_ls(core, message, session, None)
            return

        if text.startswith("/ls "):
            await handle_ls(core, message, session, text[3:].strip())
            return

        if text.startswith("/cat "):
            await handle_cat(core, message, session, text[4:].strip())
            return

        if text.startswith("/tree"):
            await handle_tree(core, message, session, text.replace("/tree", "").strip())
            return

        if text.startswith("/cd "):
            await handle_cd(core, message, session, text[3:].strip())
            return

        if text.startswith("/project "):
            parts = text.split(None, 2)
            if len(parts) < 3:
                await message.answer("📝 Использование: <code>/project &lt;имя&gt; &lt;путь&gt;</code>")
            else:
                await handle_project_set(core, message, session, parts[1], parts[2])
            return

        if text.startswith("/load "):
            await handle_project_load(core, message, session, text[5:].strip())
            return

        if text == "/projects":
            await handle_projects(core, message, session)
            return

        # ── workflow commands ─────────────────────────────────────────────────
        if text == "/plan":
            await message.answer("📝 Использование: <code>/plan &lt;задача&gt;</code>")
            return

        if text.startswith("/plan "):
            prompt = text[6:].strip()
            if not await core.guard_prompt(message, prompt):
                return
            await handle_plan(core, message, session, prompt)
            return

        if text == "/orchestrate":
            await message.answer("📝 Использование: <code>/orchestrate &lt;задача&gt;</code>")
            return

        if text.startswith("/orchestrate "):
            prompt = text[len("/orchestrate "):].strip()
            if not await core.guard_prompt(message, prompt):
                return
            await handle_orchestrate(core, message, session, prompt)
            return

        if text == "/retry_failed":
            await handle_retry_failed(core, message, session)
            return

        if text == "/recover":
            await handle_recover(core, message, session)
            return

        if text == "/review":
            await handle_review(core, message, session)
            return

        if text.startswith("/review "):
            await handle_review(core, message, session, focus=text.split(None, 1)[1].strip())
            return

        if text == "/commit":
            await handle_commit(core, message, session)
            return

        if text.startswith("/commit "):
            await handle_commit(core, message, session, commit_msg=text.split(None, 1)[1].strip())
            return

        if text.startswith("/btw "):
            question = text[4:].strip()
            if not await core.guard_prompt(message, question):
                return
            await handle_btw(core, message, session, question)
            return

        if text == "/btw":
            await message.answer("📝 Использование: <code>/btw Ваш вопрос</code>")
            return

        # ── unknown slash command ─────────────────────────────────────────────
        if text.startswith("/"):
            await message.answer(
                "❓ Неизвестная команда. Используйте /help для списка команд."
            )
            return

        # ── plain text → task ─────────────────────────────────────────────────
        if not await core.guard_prompt(message, text):
            return
        provider = session.current_provider
        await core.enqueue_task(
            session, provider, text, message,
            f"⏳ <b>Запускаю {escape(core.provider_label(provider))}…</b>",
        )

    # ── callback handler ──────────────────────────────────────────────────────

    @core.router.callback_query()
    async def _dispatch_callback(callback: CallbackQuery) -> None:
        await dispatch_callback(core, callback)

    # ── HTTP status server ────────────────────────────────────────────────────
    core.start_status_server()

    return core.bot, core.dp
