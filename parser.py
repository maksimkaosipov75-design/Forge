import re
from dataclasses import dataclass, field
from typing import List


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

        # Паттерн "готов к следующей команде"
        self.ready_prompt = re.compile(r"^>\s*$")

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

        # Детекция создания файла
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

    def get_status_text(self) -> str:
        s = self.state
        status = f"📊 Статус: {s.current_action}\n"
        if s.total_steps > 0:
            status += f"📈 Прогресс: {s.completed_steps}/{s.total_steps}\n"
        if s.files_touched:
            status += f"📁 Файлы: {', '.join(s.files_touched[-5:])}\n"
        if s.last_error:
            status += f"❌ Ошибка: {s.last_error[:80]}\n"
        status += f"{'🟢 Занят' if s.is_busy else '🔴 Свободен'}"
        return status

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

    def mark_position(self) -> int:
        """Запоминаем текущую позицию в буфере."""
        return len(self.state.raw_buffer)

    def get_new_output(self, from_position: int) -> str:
        """Возвращает только новые строки после标记рованной позиции."""
        new_lines = self.state.raw_buffer[from_position:]
        return "\n".join(new_lines)

    def get_actionable_line(self, line: str) -> str:
        """Извлекает значимую строку действия из stream-json вывода."""
        # Stream-json уже имеет чистые события с эмодзи
        if line.startswith(("⚙️", "🧠", "💬", "🔧", "🏁")):
            # Убираем префикс типа для стриминга, оставляем содержимое
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
