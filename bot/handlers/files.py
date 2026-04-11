"""
Filesystem commands: /ls, /cat, /tree, /cd, /pwd, /project, /load, /projects
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import FSInputFile

from bot.formatting import build_file_preview_messages

if TYPE_CHECKING:
    from aiogram.types import Message
    from bot.core import BotCore
    from core.task_models import ChatSession


async def handle_ls(
    core: "BotCore", message: "Message", session: "ChatSession", path: str | None
) -> None:
    await message.answer(session.file_mgr.list_dir(path))


async def handle_pwd(
    core: "BotCore", message: "Message", session: "ChatSession"
) -> None:
    from html import escape
    await message.answer(f"📂 <code>{escape(str(session.file_mgr.get_working_dir()))}</code>")


async def handle_cat(
    core: "BotCore", message: "Message", session: "ChatSession", path: str
) -> None:
    target = Path(path)
    if not target.is_absolute():
        target = session.file_mgr.get_working_dir() / target
    target = target.resolve()
    if err := session.file_mgr._check_path_safe(target):
        await message.answer(err)
        return
    if not target.exists():
        from html import escape
        await message.answer(f"❌ File not found: <code>{escape(target.name)}</code>")
        return
    if target.stat().st_size > 50_000:
        await message.answer_document(FSInputFile(target, filename=target.name))
        return
    content = target.read_text(encoding="utf-8", errors="replace")
    active = session.active_provider or session.current_provider
    escape_fn = core.get_runtime(session, active).parser._escape_html
    for preview in build_file_preview_messages(target, content, escape_fn):
        await message.answer(preview)


async def handle_tree(
    core: "BotCore", message: "Message", session: "ChatSession", args: str
) -> None:
    result = session.file_mgr.tree(args if args else None)
    active = session.active_provider or session.current_provider
    escape_fn = core.get_runtime(session, active).parser._escape_html
    for chunk in build_file_preview_messages(Path("tree.txt"), result, escape_fn):
        text = chunk.replace("<b>tree.txt</b>\n\n", "", 1)
        await message.answer(text)


async def handle_cd(
    core: "BotCore", message: "Message", session: "ChatSession", path: str
) -> None:
    await message.answer(session.file_mgr.set_working_dir(path))


async def handle_project_set(
    core: "BotCore", message: "Message", session: "ChatSession", name: str, path: str
) -> None:
    await message.answer(session.file_mgr.set_project(name, path))


async def handle_project_load(
    core: "BotCore", message: "Message", session: "ChatSession", name: str
) -> None:
    await message.answer(session.file_mgr.load_project(name))


async def handle_projects(
    core: "BotCore", message: "Message", session: "ChatSession"
) -> None:
    await message.answer(session.file_mgr.list_projects())
