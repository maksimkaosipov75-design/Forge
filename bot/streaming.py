"""
TelegramStreamRenderer — live streaming of agent output into a Telegram message.

Design goals (mirrors Claude CLI / Gemini CLI behaviour):
- Show tool calls, thinking, and text output as they arrive, not after completion
- Throttle Telegram edits to ≤ 1 per 1.5 s (stays within Bot API rate limits)
- Thinking: compact rolling snippet while thinking, collapsed blockquote once done
- Support interaction callbacks: model questions become inline-button prompts

Thread-safety:
  on_stream_event() is called from a background thread (subprocess stdout reader
  for CLI providers; loop.call_soon_threadsafe for API providers).
  All asyncio scheduling is done via self._loop.call_soon_threadsafe().
"""

from __future__ import annotations

import asyncio
import logging
import time
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from bot.formatting import build_interaction_buttons, send_html

if TYPE_CHECKING:
    from bot.core import BotCore

log = logging.getLogger(__name__)

_EDIT_INTERVAL = 1.5        # seconds between Telegram edits
_MAX_VISIBLE_LINES = 6      # recent event lines shown in status
_MAX_THINKING_CHARS = 1000  # max chars in collapsed thinking blockquote
_THINKING_SNIPPET_LEN = 90  # max chars of rolling snippet during thinking

# These prefixes signal that thinking has finished
_THINKING_DONE_ON = ("💬 ", "🔧 ", "✏️ ", "📂 ", "👁️ ", "🐚 ", "❌ ", "🏁 ")


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _fmt_tokens(n: int) -> str:
    return f"↑ {n / 1000:.1f}k" if n >= 1000 else f"↑ {n}"


def _event_to_line(raw: str) -> str | None:
    """Convert a raw stream event into a Telegram HTML line, or None to skip."""
    if raw.startswith(("💬 ", "🧠 ", "🔢 ", "🏁 ")):
        return None  # handled separately or suppressed
    if raw.startswith("🔧 "):
        tool = raw[2:].strip()
        if "Использую: " in tool:
            tool = tool.split("Использую: ", 1)[1]
        short = (tool[:42] + "…") if len(tool) > 44 else tool
        return f"🔧 <code>{escape(short)}</code>"
    if raw.startswith(("✏️ ", "📂 ")):
        parts = raw[2:].strip().split()
        name = Path(parts[-1]).name if parts else "file"
        icon = "✏️" if raw.startswith("✏️") else "📂"
        return f"{icon} <code>{escape(name)}</code>"
    if raw.startswith("👁️ "):
        parts = raw[3:].strip().split()
        name = Path(parts[-1]).name if parts else "file"
        return f"👁 <code>{escape(name)}</code>"
    if raw.startswith("🐚 "):
        cmd = raw[2:].strip()
        for prefix in ("Запускаю: ", "Running: "):
            if cmd.startswith(prefix):
                cmd = cmd[len(prefix):]
        short = (cmd[:42] + "…") if len(cmd) > 44 else cmd
        return f"🐚 <code>{escape(short)}</code>"
    if raw.startswith("❌ "):
        return f"❌ {escape(raw[2:].strip()[:200])}"
    if raw.startswith("⚙️ "):
        return f"⚙️ <i>{escape(raw[2:].strip()[:80])}</i>"
    return None


class TelegramStreamRenderer:
    """
    Receives stream events from ExecutionService and reflects them in a
    Telegram status message via throttled edits.

    The event loop reference is captured at construction time so that
    on_stream_event() can be called safely from any thread.
    """

    def __init__(
        self,
        core: "BotCore",
        status_msg: Message,
        provider: str,
        session,
    ):
        self._core = core
        self._status_msg = status_msg
        self._provider = provider
        self._session = session

        # Capture the running loop once — used for all threadsafe scheduling.
        self._loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()

        self._event_lines: list[str] = []
        self._thinking_buf: list[str] = []
        self._thinking_done: bool = False
        self._token_count: int = 0
        self._start: float = time.monotonic()
        self._last_edit: float = 0.0
        self._dirty: bool = False

        # Pending call_later handle — ensures one edit per _EDIT_INTERVAL
        self._pending_edit: asyncio.TimerHandle | None = None

        # Pending interaction future
        self._interaction_future: asyncio.Future[str] | None = None
        self._interaction_msg_id: int | None = None

    # ── public callbacks ──────────────────────────────────────────────────────

    def on_stream_event(self, raw: str) -> None:
        """
        Thread-safe.  Called from the subprocess stdout reader thread (CLI
        providers) or from the event loop thread (API providers).
        """
        # ── thinking chunks ──────────────────────────────────────────────────
        if raw.startswith("🧠 "):
            chunk = raw[2:].strip()
            if chunk:
                self._thinking_buf.append(chunk)
            self._dirty = True
            self._schedule_edit()
            return

        # ── detect end of thinking phase ─────────────────────────────────────
        if self._thinking_buf and not self._thinking_done:
            if any(raw.startswith(p) for p in _THINKING_DONE_ON):
                self._thinking_done = True
                self._dirty = True

        # ── token count (silent) ─────────────────────────────────────────────
        if raw.startswith("💬 "):
            self._token_count += max(1, len(raw[2:]) // 4)
            # Token arrival = thinking is over (text streaming started)
            if self._thinking_buf and not self._thinking_done:
                self._thinking_done = True
                self._dirty = True
            self._schedule_edit()
            return

        # ── other visible events ─────────────────────────────────────────────
        line = _event_to_line(raw)
        if line:
            self._event_lines.append(line)
            if len(self._event_lines) > _MAX_VISIBLE_LINES * 2:
                self._event_lines = self._event_lines[-_MAX_VISIBLE_LINES:]
            self._dirty = True
            self._schedule_edit()

    async def on_interaction(self, kind: str, text: str) -> str | None:
        """
        Called when the model needs user input.
        Sends a prompt message and waits for the user to respond via inline
        buttons.
        """
        self._core.set_active_renderer(self._session.chat_id, self)

        self._interaction_future = self._loop.create_future()

        question_html = (
            f"❓ <b>Модель спрашивает</b>\n\n{escape(text.strip())}"
            if kind != "approval"
            else f"✅ <b>Подтверждение</b>\n\n{escape(text.strip())}"
        )
        try:
            msg = await send_html(
                self._core.bot,
                self._session.chat_id,
                question_html,
                reply_markup=build_interaction_buttons(kind),
            )
            self._interaction_msg_id = msg.message_id
        except Exception as exc:
            log.error("Failed to send interaction prompt: %s", exc)
            return None

        try:
            answer = await asyncio.wait_for(self._interaction_future, timeout=120.0)
        except asyncio.TimeoutError:
            answer = ""
        finally:
            self._core.clear_active_renderer(self._session.chat_id)
            self._interaction_future = None
            self._interaction_msg_id = None

        return (answer + "\n") if answer else "\n"

    def resolve_interaction(self, answer: str) -> None:
        """Called by the callback handler when the user presses an inline button."""
        if self._interaction_future and not self._interaction_future.done():
            self._interaction_future.set_result(answer)

    # ── throttled editing — thread-safe debounce ──────────────────────────────

    def _schedule_edit(self) -> None:
        """
        Thread-safe entry point.  Schedules _ensure_edit_scheduled on the
        event loop so all timer manipulation happens on one thread.
        """
        self._loop.call_soon_threadsafe(self._ensure_edit_scheduled)

    def _ensure_edit_scheduled(self) -> None:
        """
        Runs on the event loop thread.
        Fires an edit immediately if the interval has elapsed, or sets a
        call_later timer so the final dirty state is always flushed.
        """
        if not self._dirty:
            return
        if self._pending_edit is not None:
            return  # timer already queued
        now = time.monotonic()
        elapsed = now - self._last_edit
        if elapsed >= _EDIT_INTERVAL:
            asyncio.ensure_future(self._do_edit())
        else:
            delay = _EDIT_INTERVAL - elapsed
            self._pending_edit = self._loop.call_later(delay, self._fire_pending)

    def _fire_pending(self) -> None:
        """Called by the loop.call_later timer."""
        self._pending_edit = None
        asyncio.ensure_future(self._do_edit())

    async def _do_edit(self) -> None:
        if not self._dirty:
            return
        self._last_edit = time.monotonic()
        self._dirty = False
        text = self._build_status_text()
        try:
            await self._status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                log.debug("edit_text failed: %s", exc)
        except Exception as exc:
            log.debug("edit_text failed: %s", exc)

    # ── status text builder ───────────────────────────────────────────────────

    def _build_status_text(self) -> str:
        elapsed = _fmt_elapsed(time.monotonic() - self._start)
        tokens = _fmt_tokens(self._token_count) if self._token_count else ""
        meta = f"  <i>{elapsed}" + (f" · {tokens}" if tokens else "") + "</i>"

        # Prefer session.active_provider so the label stays correct during
        # orchestration when different subtasks run on different providers.
        current_provider = (
            self._session.active_provider
            if getattr(self._session, "active_provider", "")
            else self._provider
        )
        parts: list[str] = [f"⏳ <b>{escape(current_provider)}</b>{meta}"]

        if self._thinking_buf:
            thinking_text = " ".join(self._thinking_buf).strip()
            if self._thinking_done:
                # ── thinking complete: collapsed expandable blockquote ────────
                if len(thinking_text) > _MAX_THINKING_CHARS:
                    thinking_text = "…" + thinking_text[-_MAX_THINKING_CHARS:]
                parts.append(
                    f"💭 <blockquote expandable>{escape(thinking_text)}</blockquote>"
                )
            else:
                # ── thinking in progress: rolling compact snippet ─────────────
                snippet = thinking_text
                if len(snippet) > _THINKING_SNIPPET_LEN:
                    snippet = "…" + snippet[-_THINKING_SNIPPET_LEN:]
                parts.append(f"💭 <i>{escape(snippet)}</i>")

        # Recent tool/file/shell event lines
        visible = self._event_lines[-_MAX_VISIBLE_LINES:]
        if visible:
            parts.append("\n".join(visible))

        return "\n\n".join(parts)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def finalize(self) -> None:
        """
        Force a final edit so the status message reflects the terminal state
        before the task result replaces it.
        """
        if self._pending_edit is not None:
            self._pending_edit.cancel()
            self._pending_edit = None
        if self._dirty:
            await self._do_edit()
