import re
from html import escape
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum


class ActionCategory(Enum):
    THINKING = "thinking"
    TEXT = "text"
    TOOL = "tool"
    SYSTEM = "system"
    DONE = "done"


@dataclass
class StreamEvent:
    """Одно событие стриминга."""
    category: ActionCategory
    text: str
    tool_name: Optional[str] = None
    file_path: Optional[str] = None


@dataclass
class AgentState:
    current_action: str = "Ожидание команды"
    total_steps: int = 0
    completed_steps: int = 0
    last_error: str = ""
    files_touched: List[str] = field(default_factory=list)
    commands_run: List[str] = field(default_factory=list)
    raw_buffer: List[str] = field(default_factory=list)
    is_busy: bool = False
    # Группированные события для прогресс-бара
    events: List[StreamEvent] = field(default_factory=list)
    tool_use_count: int = 0
    last_file_action: str = ""
    last_file_path: str = ""
    last_tool_name: str = ""


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
            self.state.current_action = "Готов к команде"
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
                    action = "✏️ Редактирует"
                    self.state.last_file_action = action
                    self.state.last_file_path = event.file_path or "?"
                    if event.file_path and event.file_path not in self.state.files_touched:
                        self.state.files_touched.append(event.file_path)
                    self.state.current_action = f"{action} {event.file_path or '?'}"

                elif event.tool_name in ("read_file", "list_directory", "glob"):
                    self.state.last_file_action = "👁️ Читает"
                    self.state.last_file_path = event.file_path or "?"
                    self.state.current_action = f"👁️ Читает {event.file_path or '?'}"

                elif event.tool_name == "run_shell_command":
                    self.state.current_action = "🐚 Запускает команду"
                    if event.text:
                        cmd = event.text[:50]
                        self.state.commands_run.append(cmd)

                elif event.tool_name in ("todo_write", "agent"):
                    self.state.current_action = f"🔧 {event.tool_name}"

            elif event.category == ActionCategory.THINKING:
                # Thinking в статус не выносим, чтобы не засорять UI.
                pass

            elif event.category == ActionCategory.DONE:
                self.state.current_action = "✅ Завершено"
                self.state.is_busy = False

            elif event.category == ActionCategory.SYSTEM:
                self.state.current_action = "🔄 Инициализация..."
                self.state.is_busy = True

        # Детекция создания файла (фоллбэк для обычного текста)
        if m := self.file_create_pattern.search(line):
            fname = m.group(1).strip("`'")
            if fname not in self.state.files_touched:
                self.state.files_touched.append(fname)
            self.state.current_action = f"📂 Создал: {fname}"
            self.state.is_busy = True

        # Детекция редактирования
        if m := self.file_edit_pattern.search(line):
            fname = m.group(1).strip("`'")
            if fname not in self.state.files_touched:
                self.state.files_touched.append(fname)
            self.state.current_action = f"✏️ Редактирует: {fname}"
            self.state.is_busy = True

        # Детекция чтения
        if m := self.file_read_pattern.search(line):
            fname = m.group(1).strip("`'")
            self.state.current_action = f"👁️ Читает: {fname}"
            self.state.is_busy = True

        # Детекция shell-команд
        if m := self.shell_cmd_pattern.search(line):
            cmd = m.group(1).strip("`'")
            self.state.commands_run.append(cmd)
            self.state.current_action = f"🐚 Запускаю: {cmd[:50]}"
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
            self.state.current_action = "✅ Задача завершена"
            self.state.is_busy = False

    def _parse_stream_event(self, line: str) -> Optional[StreamEvent]:
        """Парсит строку как stream-json событие."""
        if line.startswith("🧠 "):
            return StreamEvent(category=ActionCategory.THINKING, text=line[2:].strip())
        elif line.startswith("💬 "):
            return StreamEvent(category=ActionCategory.TEXT, text=line[2:].strip())
        elif line.startswith("🔧 Использую: "):
            tool = line[len("🔧 Использую: "):].strip()
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
        status = f"📊 Статус: {escape(s.current_action)}\n"

        # Прогресс-бар
        if s.is_busy and s.tool_use_count > 0:
            blocks = min(s.tool_use_count, 10)
            bar = "█" * blocks + "░" * (10 - blocks)
            status += f"[{bar}] {s.tool_use_count} действий\n"

        if s.total_steps > 0:
            status += f"📈 Прогресс: {s.completed_steps}/{s.total_steps}\n"
        if s.files_touched:
            status += f"📁 Файлы: {escape(', '.join(s.files_touched[-5:]))}\n"
        if s.last_error:
            status += f"❌ Ошибка: {escape(s.last_error[:80])}\n"
        status += f"{'🟢 Занят' if s.is_busy else '🔴 Свободен'}"
        return status

    def get_progress_summary(self) -> str:
        """Формирует компактное резюме для стриминга."""
        s = self.state

        parts = []

        if s.current_action and s.current_action != "Ожидание команды":
            parts.append(f"<b>{s.current_action}</b>")

        if s.is_busy and s.tool_use_count > 0:
            blocks = min(s.tool_use_count, 10)
            bar = "█" * blocks + "░" * (10 - blocks)
            parts.append(f"<code>[{bar}]</code> использовано инструментов: {s.tool_use_count}")

        if s.last_tool_name:
            parts.append(f"🔧 Текущий шаг: <code>{self._escape_html(self._shorten(s.last_tool_name, 48))}</code>")

        if s.last_file_path:
            parts.append(f"📂 Файл: <code>{self._escape_html(self._shorten(s.last_file_path, 72))}</code>")

        # Include last model commentary line so callers can show what the model said
        recent_text = [e.text for e in s.events if e.category == ActionCategory.TEXT]
        if recent_text:
            parts.append(f"💬 {self._escape_html(self._shorten(recent_text[-1], 240))}")

        return "\n".join(parts)

    def get_recent_actions(self, count: int = 5) -> List[StreamEvent]:
        """Последние N значимых действий (без thinking)."""
        meaningful = [
            e for e in self.state.events
            if e.category in (ActionCategory.TOOL, ActionCategory.DONE, ActionCategory.SYSTEM)
        ]
        return meaningful[-count:]

    def get_context_for_btw(self) -> str:
        return "\n".join(self.state.raw_buffer[-20:])

    def get_full_response(self) -> str:
        """Возвращает полный вывод qwen. Приоритет: финальный result > буфер."""
        if self.final_result:
            return self.final_result
        # Фоллбэк: собираем все 💬 строки из буфера
        text_lines = []
        for line in self.full_buffer:
            if line.startswith("💬 "):
                text_lines.append(line[2:])  # Убираем эмодзи
        return "\n".join(text_lines) if text_lines else "\n".join(self.full_buffer)

    def set_final_result(self, text: str):
        """Устанавливает финальный полный ответ из result события."""
        self.final_result = text

    def clear_full_buffer(self):
        """Очищает полный буфер для новой задачи."""
        self.full_buffer.clear()
        self.final_result = ""
        self.state.raw_buffer.clear()
        self.state.events.clear()
        self.state.tool_use_count = 0
        self.state.last_file_action = ""
        self.state.last_file_path = ""
        self.state.last_tool_name = ""
        self.state.current_action = "Ожидание команды"
        self.state.completed_steps = 0
        self.state.total_steps = 0
        self.state.last_error = ""
        self.state.is_busy = False

    def mark_position(self) -> int:
        """Запоминаем текущую позицию в буфере."""
        return len(self.state.raw_buffer)

    def get_new_output(self, from_position: int) -> str:
        """Возвращает только новые строки после позиции."""
        new_lines = self.state.raw_buffer[from_position:]
        return "\n".join(new_lines)

    def get_actionable_line(self, line: str) -> str:
        """Извлекает значимую строку действия из stream-json вывода."""
        # Stream-json уже имеет чистые события с эмодзи
        if line.startswith(("⚙️", "🧠", "💬", "🔧", "🏁", "🔢", "✏️", "📂", "👁️", "🐚", "❌", "✅")):
            return line
        if line.startswith("__READY__"):
            return None

        # Фоллбэк для обычного текста
        if m := self.file_create_pattern.search(line):
            return f"📂 Создал файл: {m.group(1).strip('`\'')}"
        if m := self.file_edit_pattern.search(line):
            return f"✏️ Редактирую: {m.group(1).strip('`\'')}"
        if m := self.file_read_pattern.search(line):
            return f"👁️ Читаю: {m.group(1).strip('`\'')}"
        if m := self.shell_cmd_pattern.search(line):
            return f"🐚 Запускаю: {m.group(1).strip('`\'')[:60]}"
        if self.error_pattern.search(line):
            return f"❌ {line[:120]}"
        if self.done_patterns.search(line):
            return f"✅ {line.strip()}"
        return None

    def format_final_response(self, result_text: str, files_new: list = None, files_changed: list = None) -> str:
        """Форматирует финальный ответ для Telegram HTML."""
        parts = []

        if files_new:
            parts.append("<b>📂 Созданы файлы:</b>")
            for f in files_new:
                parts.append(f"• <code>{f}</code>")
            parts.append("")

        if files_changed:
            parts.append("<b>✏️ Изменены файлы:</b>")
            for f in files_changed:
                parts.append(f"• <code>{f}</code>")
            parts.append("")

        if result_text and result_text.strip():
            # Экранируем HTML для безопасности
            escaped = self._escape_html(result_text)
            # Оборачиваем код в code blocks
            parts.append(f"<b>📋 Ответ:</b>\n\n<pre>{escaped}</pre>")

        return "\n".join(parts)

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    @staticmethod
    def _escape_html(text: str) -> str:
        """Экранирует HTML для Telegram parse_mode=HTML."""
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        return text
