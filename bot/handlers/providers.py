"""
Provider and model management:
/provider, /agents, /model, /reset-provider
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from core.providers import (
    is_supported_provider,
    list_supported_provider_names,
    normalize_provider_name,
)
from core.task_models import ChatSession

if TYPE_CHECKING:
    from aiogram.types import Message
    from bot.core import BotCore


# ── model catalog ─────────────────────────────────────────────────────────────

MODEL_CATALOG: dict[str, list[tuple[str, str]]] = {
    "qwen": [
        ("qwen-coder-plus", "best quality, slower"),
        ("qwen-coder-turbo", "fast balanced [default]"),
        ("qwen2.5-coder-32b-instruct", "open-weights 32B"),
        ("qwen2.5-coder-7b-instruct", "open-weights 7B, fastest"),
        ("qwen-plus", "general purpose"),
        ("qwen-max", "highest capability"),
    ],
    "codex": [
        ("o4-mini", "fast reasoning [default]"),
        ("o3", "full reasoning, slower"),
        ("o3-mini", "lightweight reasoning"),
        ("o1", "original reasoning model"),
        ("gpt-4o", "balanced multimodal"),
        ("gpt-4o-mini", "cheapest / fastest"),
    ],
    "claude": [
        ("claude-sonnet-4-6", "best balance [default]"),
        ("claude-opus-4-6", "highest quality, slow"),
        ("claude-haiku-4-5-20251001", "fastest, cheapest"),
        ("claude-sonnet-3-5", "previous gen sonnet"),
        ("sonnet", "alias to latest sonnet"),
        ("opus", "alias to latest opus"),
        ("haiku", "alias to latest haiku"),
    ],
}


def _model_label(session: ChatSession, provider: str, runtime) -> str:
    configured = session.provider_models.get(provider, "").strip()
    if configured:
        return configured
    if runtime and getattr(runtime.manager, "model_name", ""):
        return runtime.manager.model_name
    return "default"


# ── handlers ──────────────────────────────────────────────────────────────────

async def handle_provider_panel(
    core: "BotCore", message: "Message", session: ChatSession
) -> None:
    queue_info = core.queued_count(session)
    provider_cmds = ", ".join(
        f"<code>/provider {n}</code>" for n in list_supported_provider_names()
    )
    model_lines = "\n".join(
        f"• <b>{escape(n)}</b>: <code>{escape(_model_label(session, n, session.runtimes.get(n)))}</code>"
        for n in list_supported_provider_names()
    )
    await message.answer(
        f"🤖 Default provider: <b>{escape(session.current_provider)}</b>\n"
        f"▶️ Active: <b>{escape(session.active_provider or session.current_provider)}</b>\n"
        f"🕘 Queued: {queue_info}\n\n"
        f"<b>Models</b>\n{model_lines}\n\n"
        f"Switch: {provider_cmds}.",
        reply_markup=core.provider_keyboard(session),
    )


async def handle_set_provider(
    core: "BotCore", message: "Message", session: ChatSession, name: str
) -> None:
    if not is_supported_provider(name):
        providers = ", ".join(f"<code>{n}</code>" for n in list_supported_provider_names())
        await message.answer(f"❌ Available providers: {providers}.")
        return
    session.current_provider = normalize_provider_name(name)
    core.session_store.save(session)
    await message.answer(
        f"✅ Default provider switched to <b>{escape(name)}</b>.",
        reply_markup=core.provider_keyboard(session),
    )


async def handle_reset_provider(
    core: "BotCore", message: "Message", session: ChatSession
) -> None:
    session.current_provider = core.default_provider
    core.session_store.save(session)
    await message.answer(
        f"↩️ Provider reset to <b>{escape(core.default_provider)}</b>.",
        reply_markup=core.provider_keyboard(session),
    )


async def handle_model_overview(
    core: "BotCore",
    message: "Message",
    session: ChatSession,
    target_provider: str | None = None,
) -> None:
    providers = [target_provider] if target_provider else list_supported_provider_names()
    sections = ["<b>🧠 Provider models</b>"]
    for name in providers:
        rt = session.runtimes.get(name)
        current = _model_label(session, name, rt)
        sections.append(f"<b>{escape(name)}</b>: <code>{escape(current)}</code>")
        catalog = MODEL_CATALOG.get(name, [])
        if catalog:
            sections.append(
                "\n".join(
                    f"• <code>{escape(m)}</code> — {escape(d)}"
                    for m, d in catalog
                )
            )
    sections.append(
        "Usage: <code>/model qwen qwen-coder-plus</code> or "
        "<code>/model codex default</code>."
    )
    await core.send_structured(message, sections)


async def handle_model_set(
    core: "BotCore",
    message: "Message",
    session: ChatSession,
    target_provider: str,
    new_model: str,
) -> None:
    if new_model.lower() == "default":
        new_model = ""
    session.provider_models[target_provider] = new_model
    # Reset runtime so the new model is picked up on next start
    runtime = session.runtimes.pop(target_provider, None)
    if runtime and runtime.manager.is_running:
        await runtime.manager.stop()
    core.session_store.save(session)
    label = new_model or "default"
    await message.answer(
        f"🧠 Model for <b>{escape(target_provider)}</b> set to "
        f"<code>{escape(label)}</code>. Next run will use it automatically."
    )
