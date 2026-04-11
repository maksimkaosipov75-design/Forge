import re
from html import escape
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum

from core.event_protocol import decode_forge_event


class ActionCategory(Enum):
    THINKING = "thinking"
    TEXT = "text"
    TOOL = "tool"
    SYSTEM = "system"
    DONE = "done"
    QUESTION = "question"
    APPROVAL = "approval"
    PROMPT = "prompt"


@dataclass
class StreamEvent:
    """A single streaming event."""
    category: ActionCategory
    text: str
    tool_name: Optional[str] = None
    file_path: Optional[str] = None
    payload: Optional[dict] = None


@dataclass
class AgentState:
    current_action: str = "Waiting for command"
    total_steps: int = 0
    completed_steps: int = 0
    last_error: str = ""
    files_touched: List[str] = field(default_factory=list)
    commands_run: List[str] = field(default_factory=list)
    raw_buffer: List[str] = field(default_factory=list)
    is_busy: bool = False
    # Grouped events for progress bar
    events: List[StreamEvent] = field(default_factory=list)
    tool_use_count: int = 0
    last_file_action: str = ""
    last_file_path: str = ""
    last_tool_name: str = ""
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


class LogParser:
    def __init__(self, max_buffer=100):
        self.state = AgentState()
        self.max_buffer = max_buffer
        # Полный буфер без ограничений — для финального ответа
        self.full_buffer: List[str] = []
        # Финальный полный ответ из result события
        self.final_result: str = ""

        # Паттерны действий qwen
        self.file_create_pattern = re.compile(
            r"(?:Created|Writing|Creating)\s+(?:file\s+)?[`']?([^\s`']+)", re.IGNORECASE
        )
        self.file_edit_pattern = re.compile(
            r"(?:Edited|Editing|Modifying|Updating)\s+(?:file\s+)?[`']?([^\s`']+)", re.IGNORECASE
        )
        self.file_read_pattern = re.compile(
            r"(?:Reading|Opened|Opening)\s+(?:file\s+)?[`']?([^\s`']+)", re.IGNORECASE
        )
        self.shell_cmd_pattern = re.compile(
            r"(?:Running|Executing|Command|Shell):\s*[`']?([^\n`']+)", re.IGNORECASE
        )
        self.step_pattern = re.compile(
            r"(?:Step|Phase|Task)\s*(\d+)\s*(?:of|/)\s*(\d+)", re.IGNORECASE
        )
        self.error_pattern = re.compile(
            r"(Error|Exception|Failed|Traceback)", re.IGNORECASE
        )
        self.done_patterns = re.compile(
            r"(Done|Completed|Finished|Successfully|Готово|Завершено)", re.IGNORECASE
        )

    def feed(self, line: str):
        # Спец-маркер готовности
        if line == "__READY__":
            self.state.is_busy = False
            self.state.current_action = "Ready"
            return

        self.state.raw_buffer.append(line)
        if len(self.state.raw_buffer) > self.max_buffer:
            self.state.raw_buffer.pop(0)

        # Полный буфер — без ограничений
        self.full_buffer.append(line)

        # Парсим stream-json события
        event = self._parse_stream_event(line)
        if event:
            self.state.events.append(event)

            if event.category == ActionCategory.TOOL:
                self.state.tool_use_count += 1
                self.state.is_busy = True
                self.state.last_tool_name = event.tool_name or event.text or "tool"

                # Определяем тип инструмента и файл
                if event.tool_name in ("write_file", "edit"):
                    action = "✏️ Editing"
                    self.state.last_file_action = action
                    self.state.last_file_path = event.file_path or "?"
                    if event.file_path and event.file_path not in self.state.files_touched:
                        self.state.files_touched.append(event.file_path)
                    self.state.current_action = f"{action} {event.file_path or '?'}"

                elif event.tool_name in ("read_file", "list_directory", "glob"):
                    self.state.last_file_action = "👁️ Reading"
                    self.state.last_file_path = event.file_path or "?"
                    self.state.current_action = f"👁️ Reading {event.file_path or '?'}"

                elif event.tool_name == "run_shell_command":
                    self.state.current_action = "🐚 Running command"
                    if event.text:
                        cmd = event.text[:50]
                        self.state.commands_run.append(cmd)

                elif event.tool_name in ("todo_write", "agent"):
                    self.state.current_action = f"🔧 {event.tool_name}"

            elif event.category == ActionCategory.THINKING:
                # Thinking в статус не выносим, чтобы не засорять UI.
                pass

            elif event.category == ActionCategory.DONE:
                self.state.current_action = "✅ Done"
                self.state.is_busy = False

            elif event.category == ActionCategory.SYSTEM:
                self.state.current_action = "🔄 Initializing..."
                self.state.is_busy = True

        # Детекция создания файла (фоллбэк для обычного текста)
        if m := self.file_create_pattern.search(line):
            fname = m.group(1).strip("`'")
            if fname not in self.state.files_touched:
                self.state.files_touched.append(fname)
            self.state.current_action = f"📂 Created: {fname}"
            self.state.is_busy = True

        # Детекция редактирования
        if m := self.file_edit_pattern.search(line):
            fname = m.group(1).strip("`'")
            if fname not in self.state.files_touched:
                self.state.files_touched.append(fname)
            self.state.current_action = f"✏️ Editing: {fname}"
            self.state.is_busy = True

        # Детекция чтения
        if m := self.file_read_pattern.search(line):
            fname = m.group(1).strip("`'")
            self.state.current_action = f"👁️ Reading: {fname}"
            self.state.is_busy = True

        # Детекция shell-команд
        if m := self.shell_cmd_pattern.search(line):
            cmd = m.group(1).strip("`'")
            self.state.commands_run.append(cmd)
            self.state.current_action = f"🐚 Running: {cmd[:50]}"
            self.state.is_busy = True

        # Прогресс по шагам
        if m := self.step_pattern.search(line):
            self.state.completed_steps = int(m.group(1))
            self.state.total_steps = int(m.group(2))

        # Ошибки
        if self.error_pattern.search(line):
            self.state.last_error = line[:150]
            self.state.is_busy = True

        # Завершение
        if self.done_patterns.search(line):
            self.state.current_action = "✅ Task complete"
            self.state.is_busy = False

        if line.startswith("🔢 "):
            payload = line[2:].strip()
            parts = [item.strip() for item in payload.split(",", maxsplit=1)]
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                input_tokens = int(parts[0])
                output_tokens = int(parts[1])
                self.state.last_input_tokens = input_tokens
                self.state.last_output_tokens = output_tokens
                self.state.total_input_tokens += input_tokens
                self.state.total_output_tokens += output_tokens

    def _parse_stream_event(self, line: str) -> Optional[StreamEvent]:
        """Parses a line as a stream-json event."""
        forge_event = decode_forge_event(line)
        if forge_event:
            event_type = str(forge_event.get("type") or "").strip().lower()
            text = str(forge_event.get("text") or forge_event.get("message") or "").strip()
            if event_type == "thinking":
                return StreamEvent(category=ActionCategory.THINKING, text=text, payload=forge_event)
            if event_type == "question":
                return StreamEvent(category=ActionCategory.QUESTION, text=text, payload=forge_event)
            if event_type == "approval":
                return StreamEvent(category=ActionCategory.APPROVAL, text=text, payload=forge_event)
            if event_type == "prompt":
                return StreamEvent(category=ActionCategory.PROMPT, text=text, payload=forge_event)
            if event_type == "result":
                return StreamEvent(category=ActionCategory.DONE, text=text, payload=forge_event)
        if line.startswith("🧠 "):
            return StreamEvent(category=ActionCategory.THINKING, text=line[2:].strip())
        elif line.startswith("💬 "):
            return StreamEvent(category=ActionCategory.TEXT, text=line[2:].strip())
        elif line.startswith("🔧 Using: "):
            tool = line[len("🔧 Using: "):].strip()
            return StreamEvent(category=ActionCategory.TOOL, text=tool, tool_name=tool.lower())
        elif line.startswith("🔧 "):
            tool_text = line[2:].strip()
            return StreamEvent(category=ActionCategory.TOOL, text=tool_text, tool_name=tool_text.lower())
        elif line.startswith("⚙️ "):
            return StreamEvent(category=ActionCategory.SYSTEM, text=line[2:].strip())
        elif line.startswith("🏁 "):
            return StreamEvent(category=ActionCategory.DONE, text=line[2:].strip())
        return None

    def get_status_text(self) -> str:
        s = self.state
        status = f"📊 Status: {escape(s.current_action)}\n"

        # Прогресс-бар
        if s.is_busy and s.tool_use_count > 0:
            blocks = min(s.tool_use_count, 10)
            bar = "█" * blocks + "░" * (10 - blocks)
            status += f"[{bar}] {s.tool_use_count} actions\n"

        if s.total_steps > 0:
            status += f"📈 Progress: {s.completed_steps}/{s.total_steps}\n"
        if s.files_touched:
            status += f"📁 Files: {escape(', '.join(s.files_touched[-5:]))}\n"
        if s.last_error:
            status += f"❌ Error: {escape(s.last_error[:80])}\n"
        status += f"{'🟢 Busy' if s.is_busy else '🔴 Idle'}"
        return status

    def get_progress_summary(self) -> str:
        """Builds a compact streaming summary."""
        s = self.state

        parts = []

        if s.current_action and s.current_action != "Waiting for command":
            parts.append(f"<b>{s.current_action}</b>")

        if s.is_busy and s.tool_use_count > 0:
            blocks = min(s.tool_use_count, 10)
            bar = "█" * blocks + "░" * (10 - blocks)
            parts.append(f"<code>[{bar}]</code> tools used: {s.tool_use_count}")

        if s.last_tool_name:
            parts.append(f"🔧 Current step: <code>{self._escape_html(self._shorten(s.last_tool_name, 48))}</code>")

        if s.last_file_path:
            parts.append(f"📂 File: <code>{self._escape_html(self._shorten(s.last_file_path, 72))}</code>")

        # Include last model commentary line so callers can show what the model said
        recent_text = [e.text for e in s.events if e.category == ActionCategory.TEXT]
        if recent_text:
            parts.append(f"💬 {self._escape_html(self._shorten(recent_text[-1], 240))}")

        return "\n".join(parts)

    def get_recent_actions(self, count: int = 5) -> List[StreamEvent]:
        """Last N significant actions (excluding thinking)."""
        meaningful = [
            e for e in self.state.events
            if e.category in (ActionCategory.TOOL, ActionCategory.DONE, ActionCategory.SYSTEM)
        ]
        return meaningful[-count:]

    def get_context_for_btw(self) -> str:
        return "\n".join(self.state.raw_buffer[-20:])

    def get_full_response(self) -> str:
        """Returns full qwen output. Priority: final result > buffer."""
        if self.final_result:
            return self.final_result
        # Fallback: collect all 💬 text lines from the buffer.
        text_lines = []
        for line in self.full_buffer:
            if line.startswith("💬 "):
                text_lines.append(line[2:])
        if text_lines:
            return "\n".join(text_lines)
        # Last-resort fallback: raw buffer, but skip protocol/metadata lines
        # (FORGE_EVENT, 🧠 thinking, 🏁 done, 🔢 usage) so they never leak
        # into the displayed answer.
        plain = [
            line for line in self.full_buffer
            if line
            and not line.startswith("FORGE_EVENT ")
            and not line.startswith("🧠 ")
            and not line.startswith("🏁 ")
            and not line.startswith("🔢 ")
            and not line.startswith("__READY__")
        ]
        return "\n".join(plain)

    def set_final_result(self, text: str):
        """Sets the final full answer from a result event."""
        self.final_result = text

    def clear_full_buffer(self):
        """Clears the full buffer for a new task."""
        self.full_buffer.clear()
        self.final_result = ""
        self.state.raw_buffer.clear()
        self.state.events.clear()
        self.state.tool_use_count = 0
        self.state.last_file_action = ""
        self.state.last_file_path = ""
        self.state.last_tool_name = ""
        self.state.last_input_tokens = 0
        self.state.last_output_tokens = 0
        self.state.current_action = "Waiting for command"
        self.state.completed_steps = 0
        self.state.total_steps = 0
        self.state.last_error = ""
        self.state.is_busy = False

    def mark_position(self) -> int:
        """Snapshot the current buffer position."""
        return len(self.state.raw_buffer)

    def get_new_output(self, from_position: int) -> str:
        """Returns only new lines after the snapshot position."""
        new_lines = self.state.raw_buffer[from_position:]
        return "\n".join(new_lines)

    def get_actionable_line(self, line: str) -> str:
        """Extracts a meaningful action line from stream-json output."""
        forge_event = decode_forge_event(line)
        if forge_event:
            event_type = str(forge_event.get("type") or "").strip().lower()
            text = str(forge_event.get("text") or forge_event.get("message") or "").strip()
            if event_type == "thinking":
                return f"🧠 {text}" if text else "🧠 thinking"
            if event_type == "question":
                title = str(forge_event.get("title") or "").strip()
                summary = title or text or "question"
                return f"❓ {summary}"
            if event_type == "approval":
                title = str(forge_event.get("title") or "").strip()
                summary = title or text or "approval"
                return f"✅ {summary}"
            if event_type == "prompt":
                title = str(forge_event.get("title") or "").strip()
                summary = title or text or "prompt"
                return f"💭 {summary}"
            if event_type == "result":
                return f"🏁 {text}" if text else "🏁 result"
        # Stream-json уже имеет чистые события с эмодзи
        if line.startswith(("⚙️", "🧠", "💬", "🔧", "🏁", "🔢", "✏️", "📂", "👁️", "🐚", "❌", "✅")):
            return line
        if line.startswith("__READY__"):
            return None

        # Фоллбэк для обычного текста
        if m := self.file_create_pattern.search(line):
            return f"📂 Created: {m.group(1).strip('`\'')}"
        if m := self.file_edit_pattern.search(line):
            return f"✏️ Editing: {m.group(1).strip('`\'')}"
        if m := self.file_read_pattern.search(line):
            return f"👁️ Reading: {m.group(1).strip('`\'')}"
        if m := self.shell_cmd_pattern.search(line):
            return f"🐚 Running: {m.group(1).strip('`\'')[:60]}"
        if self.error_pattern.search(line):
            return f"❌ {line[:120]}"
        if self.done_patterns.search(line):
            return f"✅ {line.strip()}"
        return None

    def format_final_response(self, result_text: str, files_new: list = None, files_changed: list = None) -> str:
        """Formats the final answer as Telegram HTML."""
        parts = []

        if files_new:
            parts.append("<b>📂 Created files:</b>")
            for f in files_new:
                parts.append(f"• <code>{f}</code>")
            parts.append("")

        if files_changed:
            parts.append("<b>✏️ Changed files:</b>")
            for f in files_changed:
                parts.append(f"• <code>{f}</code>")
            parts.append("")

        if result_text and result_text.strip():
            # Экранируем HTML для безопасности
            escaped = self._escape_html(result_text)
            # Оборачиваем код в code blocks
            parts.append(f"<b>📋 Answer:</b>\n\n<pre>{escaped}</pre>")

        return "\n".join(parts)

    def get_token_usage(self) -> tuple[int, int, int, int]:
        return (
            self.state.last_input_tokens,
            self.state.last_output_tokens,
            self.state.total_input_tokens,
            self.state.total_output_tokens,
        )

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escapes HTML for Telegram parse_mode=HTML."""
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        return text
