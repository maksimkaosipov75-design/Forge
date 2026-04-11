import logging
from html import escape
from pathlib import Path
from types import SimpleNamespace

try:
    from aiogram import Bot
    from aiogram.enums import ParseMode
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
except ModuleNotFoundError:  # pragma: no cover - allows lightweight tests without aiogram
    Bot = object
    Message = object
    ParseMode = SimpleNamespace(HTML="HTML")

    class InlineKeyboardButton:
        def __init__(self, text: str, callback_data: str | None = None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard: list[list["InlineKeyboardButton"]]):
            self.inline_keyboard = inline_keyboard


log = logging.getLogger(__name__)


def guess_language(file_path: Path) -> str:
    mapping = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "jsx",
        ".json": "json",
        ".md": "markdown",
        ".sh": "bash",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".html": "html",
        ".css": "css",
        ".toml": "toml",
        ".xml": "xml",
    }
    return mapping.get(file_path.suffix.lower(), "")


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


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

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:max_len]
            split_at = len(chunk)

        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    return chunks


def code_block(text: str, escape_html, language: str = "") -> str:
    escaped = escape_html(text)
    if language:
        return f'<pre><code class="language-{language}">{escaped}</code></pre>'
    return f"<pre>{escaped}</pre>"


def rel_display(path_str: str, base_dir: Path) -> str:
    path = Path(path_str)
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        return str(path)


def chunk_code_sections(text: str, escape_html, language: str = "", max_len: int = 3200) -> list[str]:
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in lines or [text]:
        escaped_line = escape_html(line)
        line_len = len(escaped_line)

        if current_lines and current_len + line_len > max_len:
            chunks.append(code_block("".join(current_lines), escape_html, language))
            current_lines = [line]
            current_len = line_len
            continue

        if line_len > max_len:
            for part in split_plain_text(line, max_len=max_len // 2):
                if current_lines:
                    chunks.append(code_block("".join(current_lines), escape_html, language))
                    current_lines = []
                    current_len = 0
                chunks.append(code_block(part, escape_html, language))
            continue

        current_lines.append(line)
        current_len += line_len

    if current_lines:
        chunks.append(code_block("".join(current_lines), escape_html, language))

    return chunks or [code_block(text, escape_html, language)]


def compose_html_messages(sections: list[str], max_len: int = 3800) -> list[str]:
    messages: list[str] = []
    current_parts: list[str] = []

    for section in sections:
        candidate_parts = current_parts + [section]
        candidate = "\n\n".join(candidate_parts)
        if current_parts and len(candidate) > max_len:
            messages.append("\n\n".join(current_parts))
            current_parts = [section]
        else:
            current_parts = candidate_parts

    if current_parts:
        messages.append("\n\n".join(current_parts))

    return messages or [""]


def format_task_result_sections(
    working_dir: Path,
    new_files: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> list[str]:
    parts: list[str] = ["<b>✅ Task complete</b>"]

    if new_files:
        parts.append("<b>📂 New files</b>")
        for file_path in new_files:
            parts.append(f"• <code>{escape(rel_display(file_path, working_dir))}</code>")

    if changed_files:
        parts.append("<b>✏️ Changed files</b>")
        for file_path in changed_files:
            parts.append(f"• <code>{escape(rel_display(file_path, working_dir))}</code>")

    return parts


def format_status_message(progress_html: str) -> str:
    if progress_html:
        return f"⏳ <b>Running</b>\n\n{progress_html}"
    return "⏳ <b>Running</b>\n\nPreparing task…"


def build_task_buttons(
    work_dir: Path,
    new_files: list[str],
    changed_files: list[str],
    can_retry_failed: bool = False,
) -> InlineKeyboardMarkup:
    keyboard_rows = []
    all_files = list(dict.fromkeys(new_files + changed_files))

    for file_path in all_files[:3]:
        try:
            label = str(Path(file_path).relative_to(work_dir))
        except ValueError:
            label = Path(file_path).name
        keyboard_rows.append([
            InlineKeyboardButton(
                text=f"📄 {truncate_text(label, 28)}",
                callback_data=f"view_file:{file_path}",
            )
        ])

    action_row = [InlineKeyboardButton(text="🔄 Repeat", callback_data="repeat_task")]
    if can_retry_failed:
        action_row.append(InlineKeyboardButton(text="♻️ Retry Failed", callback_data="retry_failed_subtask"))
    keyboard_rows.append(action_row)
    keyboard_rows.append([
        InlineKeyboardButton(text="ℹ️ Details", callback_data="show_details"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def build_plan_preview_buttons(can_run: bool = True) -> InlineKeyboardMarkup:
    action_row = []
    if can_run:
        action_row.append(InlineKeyboardButton(text="▶️ Run plan", callback_data="plan_run"))
    action_row.append(InlineKeyboardButton(text="✏️ Edit", callback_data="plan_edit"))
    action_row.append(InlineKeyboardButton(text="❌ Cancel", callback_data="plan_cancel"))
    return InlineKeyboardMarkup(inline_keyboard=[action_row])


async def send_html_message(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


async def send_answer_chunks(
    bot: Bot,
    anchor_message: Message,
    answer_text: str,
    escape_html,
    title: str = "<b>📋 Agent response</b>",
    skip_first_chunk: bool = False,
):
    if not answer_text or not answer_text.strip():
        return

    chunks = chunk_code_sections(answer_text, escape_html)
    start_index = 1 if skip_first_chunk else 0
    for idx, chunk in enumerate(chunks[start_index:], start=start_index):
        prefix = title if idx == 0 else "<b>📋 Agent response</b> <i>(continued)</i>"
        try:
            await send_html_message(bot, anchor_message.chat.id, f"{prefix}\n\n{chunk}")
        except Exception as exc:
            log.exception("Failed to send answer chunk: %s", exc)
            escaped_text = escape_html(answer_text)
            for plain_chunk in split_plain_text(escaped_text, max_len=3500):
                await send_html_message(bot, anchor_message.chat.id, f"{prefix}\n\n<pre>{plain_chunk}</pre>")
            return


async def send_or_edit_structured_message(
    bot: Bot,
    anchor_message: Message,
    target_message: Message,
    sections: list[str],
    reply_markup: InlineKeyboardMarkup | None = None,
):
    chunks = compose_html_messages(sections)
    first_chunk, tail = chunks[0], chunks[1:]

    try:
        await target_message.edit_text(first_chunk, reply_markup=reply_markup)
    except Exception as exc:
        log.exception("Failed to edit result message: %s", exc)
        await send_html_message(bot, anchor_message.chat.id, first_chunk, reply_markup=reply_markup)

    for chunk in tail:
        try:
            await send_html_message(bot, anchor_message.chat.id, chunk)
        except Exception as exc:
            log.exception("Failed to send structured chunk: %s", exc)
            fallback = escape(chunk)
            for plain_chunk in split_plain_text(fallback, max_len=3500):
                await send_html_message(bot, anchor_message.chat.id, f"<pre>{plain_chunk}</pre>")


def build_file_preview_messages(file_path: Path, content: str, escape_html) -> list[str]:
    language = guess_language(file_path)
    header = f"<b>{escape(file_path.name)}</b>"
    chunks = split_plain_text(content, max_len=3200)
    messages: list[str] = []
    for idx, chunk in enumerate(chunks):
        title = header if idx == 0 else f"{header} <i>(continued)</i>"
        messages.append(f"{title}\n\n{code_block(chunk, escape_html, language)}")
    return messages
