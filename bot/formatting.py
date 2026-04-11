"""
Telegram message formatting helpers.

Replaces telegram_ui.py with improved support for:
- Thinking blocks via <blockquote expandable> (Bot API 7.4+)
- Stream event rendering for live status messages
- Better result layout
"""

from __future__ import annotations

import logging
from html import escape
from pathlib import Path
from types import SimpleNamespace

from bot.file_registry import register as _reg_file

log = logging.getLogger(__name__)

try:
    from aiogram import Bot
    from aiogram.enums import ParseMode
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
except ModuleNotFoundError:  # pragma: no cover
    Bot = object
    Message = object
    ParseMode = SimpleNamespace(HTML="HTML")

    class InlineKeyboardButton:
        def __init__(self, text: str, callback_data: str | None = None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard


# ── language detection ────────────────────────────────────────────────────────

_LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".json": "json", ".md": "markdown",
    ".sh": "bash", ".yml": "yaml", ".yaml": "yaml", ".html": "html",
    ".css": "css", ".toml": "toml", ".xml": "xml", ".rs": "rust",
    ".go": "go", ".sql": "sql", ".diff": "diff",
}


def guess_language(file_path: Path) -> str:
    return _LANG_MAP.get(file_path.suffix.lower(), "")


# ── text utilities ────────────────────────────────────────────────────────────

def truncate_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_plain_text(text: str, max_len: int = 3500) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = remaining.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunk = remaining[:split_at].rstrip() or remaining[:max_len]
        chunks.append(chunk)
        remaining = remaining[len(chunk):].lstrip()
    return chunks


def rel_display(path_str: str, base_dir: Path) -> str:
    try:
        return str(Path(path_str).relative_to(base_dir))
    except ValueError:
        return str(Path(path_str).name)


# ── HTML block builders ───────────────────────────────────────────────────────

def code_block(text: str, escape_fn, language: str = "") -> str:
    escaped = escape_fn(text)
    if language:
        return f'<pre><code class="language-{language}">{escaped}</code></pre>'
    return f"<pre>{escaped}</pre>"


def thinking_block(text: str) -> str:
    """Render thinking text as a Telegram expandable blockquote (Bot API 7.4+)."""
    if not text.strip():
        return ""
    escaped = escape(text.strip())
    return f"💭 <blockquote expandable>{escaped}</blockquote>"


def chunk_code_sections(
    text: str,
    escape_fn,
    language: str = "",
    max_len: int = 3200,
) -> list[str]:
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in lines or [text]:
        escaped_line = escape_fn(line)
        line_len = len(escaped_line)

        if current_lines and current_len + line_len > max_len:
            chunks.append(code_block("".join(current_lines), escape_fn, language))
            current_lines = [line]
            current_len = line_len
            continue

        if line_len > max_len:
            for part in split_plain_text(line, max_len=max_len // 2):
                if current_lines:
                    chunks.append(code_block("".join(current_lines), escape_fn, language))
                    current_lines = []
                    current_len = 0
                chunks.append(code_block(part, escape_fn, language))
            continue

        current_lines.append(line)
        current_len += line_len

    if current_lines:
        chunks.append(code_block("".join(current_lines), escape_fn, language))

    return chunks or [code_block(text, escape_fn, language)]


def compose_html_messages(sections: list[str], max_len: int = 3800) -> list[str]:
    messages: list[str] = []
    current_parts: list[str] = []
    for section in sections:
        candidate = "\n\n".join(current_parts + [section])
        if current_parts and len(candidate) > max_len:
            messages.append("\n\n".join(current_parts))
            current_parts = [section]
        else:
            current_parts.append(section)
    if current_parts:
        messages.append("\n\n".join(current_parts))
    return messages or [""]


# ── result & status sections ──────────────────────────────────────────────────

def format_task_result_sections(
    working_dir: Path,
    new_files: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> list[str]:
    parts: list[str] = ["<b>✅ Task complete</b>"]
    if new_files:
        parts.append("<b>📂 New files</b>")
        for fp in new_files:
            parts.append(f"• <code>{escape(rel_display(fp, working_dir))}</code>")
    if changed_files:
        parts.append("<b>✏️ Changed files</b>")
        for fp in changed_files:
            parts.append(f"• <code>{escape(rel_display(fp, working_dir))}</code>")
    return parts


def format_status_message(progress_html: str) -> str:
    if progress_html:
        return f"⏳ <b>Running</b>\n\n{progress_html}"
    return "⏳ <b>Running</b>\n\nPreparing task…"


# ── inline keyboards ──────────────────────────────────────────────────────────

def build_task_buttons(
    work_dir: Path,
    new_files: list[str],
    changed_files: list[str],
    can_retry_failed: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    for fp in list(dict.fromkeys(new_files + changed_files))[:3]:
        try:
            label = str(Path(fp).relative_to(work_dir))
        except ValueError:
            label = Path(fp).name
        rows.append([
            InlineKeyboardButton(
                text=f"📄 {truncate_text(label, 28)}",
                callback_data=f"view_file:{_reg_file(fp)}",
            )
        ])
    action_row = [InlineKeyboardButton(text="🔄 Repeat", callback_data="repeat_task")]
    if can_retry_failed:
        action_row.append(
            InlineKeyboardButton(text="♻️ Retry Failed", callback_data="retry_failed_subtask")
        )
    rows.append(action_row)
    rows.append([InlineKeyboardButton(text="ℹ️ Details", callback_data="show_details")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_plan_preview_buttons(can_run: bool = True) -> InlineKeyboardMarkup:
    row = []
    if can_run:
        row.append(InlineKeyboardButton(text="▶️ Run plan", callback_data="plan_run"))
    row.append(InlineKeyboardButton(text="✏️ Edit", callback_data="plan_edit"))
    row.append(InlineKeyboardButton(text="❌ Cancel", callback_data="plan_cancel"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def build_interaction_buttons(kind: str) -> InlineKeyboardMarkup:
    """Inline keyboard for model interaction prompts (questions / approvals)."""
    if kind == "approval":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes", callback_data="interaction:yes"),
            InlineKeyboardButton(text="❌ No", callback_data="interaction:no"),
        ]])
    # free-text question — only a skip button; user types their answer
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Skip", callback_data="interaction:skip"),
    ]])


# ── message senders ───────────────────────────────────────────────────────────

async def send_html(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


async def send_answer_chunks(
    bot: Bot,
    anchor_message: Message,
    answer_text: str,
    escape_fn,
    title: str = "<b>📋 Agent response</b>",
    skip_first_chunk: bool = False,
):
    if not answer_text or not answer_text.strip():
        return
    chunks = chunk_code_sections(answer_text, escape_fn)
    start = 1 if skip_first_chunk else 0
    for idx, chunk in enumerate(chunks[start:], start=start):
        prefix = title if idx == 0 else f"{title} <i>(continued)</i>"
        try:
            await send_html(bot, anchor_message.chat.id, f"{prefix}\n\n{chunk}")
        except Exception as exc:
            log.exception("Failed to send answer chunk: %s", exc)
            for plain in split_plain_text(escape_fn(answer_text), max_len=3500):
                await send_html(bot, anchor_message.chat.id, f"{prefix}\n\n<pre>{plain}</pre>")
            return


async def send_or_edit_structured(
    bot: Bot,
    anchor: Message,
    target: Message,
    sections: list[str],
    reply_markup: InlineKeyboardMarkup | None = None,
):
    chunks = compose_html_messages(sections)
    first, tail = chunks[0], chunks[1:]
    try:
        await target.edit_text(first, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception:
        await send_html(bot, anchor.chat.id, first, reply_markup=reply_markup)
    for chunk in tail:
        try:
            await send_html(bot, anchor.chat.id, chunk)
        except Exception as exc:
            log.exception("Failed to send structured chunk: %s", exc)
            for plain in split_plain_text(escape(chunk), max_len=3500):
                await send_html(bot, anchor.chat.id, f"<pre>{plain}</pre>")


def build_file_preview_messages(
    file_path: Path,
    content: str,
    escape_fn,
) -> list[str]:
    language = guess_language(file_path)
    header = f"<b>{escape(file_path.name)}</b>"
    chunks = split_plain_text(content, max_len=3200)
    messages = []
    for idx, chunk in enumerate(chunks):
        title = header if idx == 0 else f"{header} <i>(continued)</i>"
        messages.append(f"{title}\n\n{code_block(chunk, escape, language)}")
    return messages
