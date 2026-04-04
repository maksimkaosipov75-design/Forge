import asyncio
import json
import logging
from pathlib import Path
from typing import Optional, Callable

log = logging.getLogger(__name__)


DEFAULT_TIMEOUT = 600  # 10 минут


class QwenProcessManager:
    """
    Запускает qwen с --output-format stream-json.
    Реальный стриминг: thinking, text, tool_calls в реальном времени.
    """

    def __init__(self, cli_path: str, on_output: Callable[[str], None], timeout: int = DEFAULT_TIMEOUT):
        self.cli_path = cli_path
        self.on_output = on_output
        self.timeout = timeout
        self._running = False
        self._session_active = False
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stream_callback: Optional[Callable[[str], None]] = None
        self._final_result_callback: Optional[Callable[[str], None]] = None

    async def start(self):
        self._running = True
        log.info("QwenProcessManager инициализирован")

    def set_stream_callback(self, callback: Optional[Callable[[str], None]]):
        self._stream_callback = callback

    def set_final_result_callback(self, callback: Optional[Callable[[str], None]]):
        self._final_result_callback = callback

    async def send_command(self, text: str, cwd: Path = None):
        if not self._running:
            raise RuntimeError("Менеджер не запущен")

        work_dir = cwd or Path.cwd()
        args = [
            self.cli_path, "--yolo",
            "--output-format", "stream-json",
            "--include-partial-messages",
        ]
        if self._session_active:
            args.append("--continue")
        args.append(text)

        log.info(f"Запуск stream-json: {' '.join(args[:5])}... в {work_dir}")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
        )
        self._proc = proc
        log.info(f"Процесс запущен PID={proc.pid}, cwd={work_dir}")

        # Читаем stdout — stream-json идёт построчно в реальном времени
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
            except asyncio.TimeoutError:
                log.error(f"Таймаут {self.timeout}с — убиваю процесс PID={proc.pid}")
                proc.kill()
                await proc.wait()
                self._proc = None
                return -1
            if not line:
                break

            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue

            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                log.debug(f"Невалидный JSON от qwen: {raw[:200]}")
                continue

            t = d.get("type", "")

            if t == "system":
                sid = d.get("session_id", "")[:8]
                event = f"⚙️ Инициализация сессии..."
                self._notify(event, raw)

            elif t == "assistant":
                msg = d.get("message", {})
                for content in msg.get("content", []):
                    ct = content.get("type", "")

                    if ct == "thinking":
                        thought = content.get("thinking", "")
                        event = f"🧠 {thought}"
                        self._notify(event, raw)

                    elif ct == "text":
                        txt = content.get("text", "")
                        event = f"💬 {txt}"
                        self._notify(event, raw)

                    elif ct == "tool_use":
                        tool = content.get("input", {})
                        tool_name = content.get("name", "tool")
                        event = f"🔧 {tool_name}"
                        self._notify(event, raw)

            elif t == "tool_use":
                tool_name = d.get("name", "tool")
                event = f"🔧 Использую: {tool_name}"
                self._notify(event, raw)

            elif t == "tool_result":
                event = "🔧 Результат инструмента"
                self._notify(event, raw)

            elif t == "result":
                sub = d.get("subtype", "")
                dur = d.get("duration_ms", 0)
                event = f"🏁 Завершено ({sub}): {dur}ms"
                self._notify(event, raw)
                # Сохраняем полный ответ
                final_text = d.get("result", "")
                if final_text and self._final_result_callback:
                    self._final_result_callback(final_text)

        # Читаем stderr
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                log.info(f"Qwen stderr: {text}")

        await proc.wait()
        self._proc = None
        self._session_active = True
        log.info(f"Процесс завершился с кодом {proc.returncode}")
        return proc.returncode

    def _notify(self, event: str, raw_json: str):
        """Уведомляет все подписчиков о новом событии."""
        log.info(f"Stream: {event[:100]}")
        # Колбэк для стриминга в Telegram
        if self._stream_callback:
            self._stream_callback(event)
        # Колбэк для парсера
        self.on_output(event)

    async def stop(self):
        if self._proc and self._proc.returncode is None:
            log.info(f"Убиваю активный процесс PID={self._proc.pid}")
            self._proc.kill()
            await self._proc.wait()
            self._proc = None
        self._running = False
        self._session_active = False
        log.info("Сессия сброшена")

    @property
    def is_running(self) -> bool:
        return self._running
