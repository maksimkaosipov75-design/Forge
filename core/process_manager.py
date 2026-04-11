import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional, Callable

from core.event_protocol import encode_forge_event, extract_forge_event
from core.provider_status import FailureReason, ProviderHealth, classify_failure_text
from core.providers import normalize_provider_name

log = logging.getLogger(__name__)


DEFAULT_TIMEOUT = 600  # 10 минут


class BaseProcessManager:
    def __init__(self, cli_path: str, on_output: Callable[[str], None], timeout: int = DEFAULT_TIMEOUT, provider_name: str = "qwen", model_name: str = ""):
        self.cli_path = cli_path
        self.on_output = on_output
        self.timeout = timeout
        self.provider_name = normalize_provider_name(provider_name)
        self.model_name: str = model_name  # "" = use CLI default
        self._running = False
        self._session_active = False
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stream_callback: Optional[Callable[[str], None]] = None
        self._final_result_callback: Optional[Callable[[str], None]] = None
        self.health = ProviderHealth(provider=self.provider_name)
        # Prevents concurrent send_command calls on the same process manager
        self._command_lock: asyncio.Lock = asyncio.Lock()

    async def start(self):
        self._running = True
        log.info("%s инициализирован", self.__class__.__name__)

    def set_stream_callback(self, callback: Optional[Callable[[str], None]]):
        self._stream_callback = callback

    def set_final_result_callback(self, callback: Optional[Callable[[str], None]]):
        self._final_result_callback = callback

    async def write_stdin(self, text: str) -> bool:
        """Write a line to the running process stdin. Returns True on success."""
        if self._proc and self._proc.stdin and self._proc.returncode is None:
            try:
                self._proc.stdin.write((text + "\n").encode())
                await self._proc.stdin.drain()
                log.info("write_stdin: sent %d bytes to PID=%s", len(text) + 1, self._proc.pid)
                return True
            except Exception as exc:
                log.warning("write_stdin failed: %s", exc)
        return False

    def _notify(self, event: str, raw_json: str):
        log.info(f"Stream: {event[:100]}")
        failure = classify_failure_text(event)
        if failure:
            self.health.register_failure(failure)
        if self._stream_callback:
            self._stream_callback(event)
        self.on_output(event)

    def mark_success(self):
        self.health.register_success()

    def mark_failure(self, text: str):
        failure = classify_failure_text(text) or FailureReason(kind="unknown", message=text or "Unknown failure", source_text=text or "")
        self.health.register_failure(failure)

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


class QwenProcessManager(BaseProcessManager):
    """
    Run qwen with --output-format stream-json.
    Streaming events include thinking, text, and tool calls in real time.
    """

    @staticmethod
    def _truncate_thinking(text: str, limit: int = 100) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    @classmethod
    def parse_stream_payload(cls, payload: dict) -> tuple[list[str], Optional[str]]:
        events: list[str] = []
        final_text: Optional[str] = None
        payload_type = payload.get("type", "")
        forge_event = extract_forge_event(payload)
        if forge_event:
            event_type = str(forge_event.get("type") or "").strip().lower()
            text = str(forge_event.get("text") or forge_event.get("message") or "").strip()
            event_payload = {k: v for k, v in forge_event.items() if k not in {"type", "text"}}
            events.append(encode_forge_event(event_type, text=text, **event_payload))
            if event_type == "result":
                final_text = text or (forge_event.get("result") if isinstance(forge_event.get("result"), str) else None)
            return events, final_text

        if payload_type == "system":
            events.append("⚙️ Инициализация сессии...")

        elif payload_type == "assistant":
            message = payload.get("message", {})
            for content in message.get("content", []):
                content_type = content.get("type", "")

                if content_type == "thinking":
                    thought = cls._truncate_thinking(content.get("thinking", ""))
                    if thought:
                        events.append(f"🧠 {thought}")

                elif content_type == "text":
                    text = content.get("text", "")
                    if text:
                        events.append(f"💬 {text}")

                elif content_type == "tool_use":
                    tool_name = content.get("name", "tool")
                    events.append(f"🔧 {tool_name}")

        elif payload_type == "stream_event":
            event = payload.get("event", {})
            event_type = event.get("type", "")

            if event_type == "content_block_start":
                content_block = event.get("content_block", {})
                block_type = content_block.get("type", "")
                if block_type == "tool_use":
                    tool_name = content_block.get("name", "tool")
                    events.append(f"🔧 {tool_name}")
                elif block_type == "thinking":
                    thought = cls._truncate_thinking(content_block.get("thinking", ""))
                    if thought:
                        events.append(f"🧠 {thought}")
                elif block_type == "text":
                    text = content_block.get("text", "")
                    if text:
                        events.append(f"💬 {text}")

            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                delta_type = delta.get("type", "")
                if delta_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        events.append(f"💬 {text}")
                elif delta_type == "thinking_delta":
                    thought = cls._truncate_thinking(delta.get("thinking", ""))
                    if thought:
                        events.append(f"🧠 {thought}")

        elif payload_type == "tool_use":
            tool_name = payload.get("name", "tool")
            events.append(f"🔧 Использую: {tool_name}")

        elif payload_type == "tool_result":
            events.append("🔧 Результат инструмента")

        elif payload_type == "result":
            subtype = payload.get("subtype", "")
            duration_ms = payload.get("duration_ms", 0)
            events.append(f"🏁 Завершено ({subtype}): {duration_ms}ms")
            final_text = payload.get("result", "") or None
            usage = payload.get("usage") or {}
            input_tokens = usage.get("input_tokens") or usage.get("inputTokens") or 0
            output_tokens = usage.get("output_tokens") or usage.get("outputTokens") or 0
            if input_tokens or output_tokens:
                events.append(f"🔢 {input_tokens},{output_tokens}")

        return events, final_text

    async def send_command(self, text: str, cwd: Path = None):
        async with self._command_lock:
            return await self._send_command_impl(text, cwd)

    async def _send_command_impl(self, text: str, cwd: Path = None):
        if not self._running:
            raise RuntimeError("Менеджер не запущен")

        work_dir = cwd or Path.cwd()
        args = [
            self.cli_path, "--yolo",
            "--output-format", "stream-json",
            "--include-partial-messages",
        ]
        if self.model_name:
            args.extend(["-m", self.model_name])
        if self._session_active:
            args.append("--continue")
        args.append(text)

        log.info(f"Запуск stream-json: {' '.join(args[:6])}... в {work_dir}")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
        )
        self._proc = proc
        log.info(f"Процесс запущен PID={proc.pid}, cwd={work_dir}")

        # Read stdout line by line because qwen emits stream-json incrementally.
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
            except asyncio.TimeoutError:
                log.error(f"Таймаут {self.timeout}с — убиваю процесс PID={proc.pid}")
                proc.kill()
                await proc.wait()
                self._proc = None
                self.mark_failure(f"Timeout after {self.timeout}s")
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

            events, final_text = self.parse_stream_payload(d)
            for event in events:
                self._notify(event, raw)

            if final_text and self._final_result_callback:
                self._final_result_callback(final_text)

        # Drain stderr after stdout is finished.
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
        if proc.returncode == 0:
            self.mark_success()
        else:
            self.mark_failure(f"Process exited with code {proc.returncode}")
        log.info(f"Процесс завершился с кодом {proc.returncode}")
        return proc.returncode

class CodexProcessManager(BaseProcessManager):
    """
    Run codex exec --json and normalize JSONL events to the same emoji events
    already consumed by the existing parser.
    """

    @staticmethod
    def _unwrap_payload(payload: dict) -> dict:
        return payload.get("msg", payload)

    @staticmethod
    def _extract_text(payload: dict) -> str:
        for key in ("text", "message", "content", "aggregated_output"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @classmethod
    def parse_stream_payload(cls, payload: dict) -> tuple[list[str], Optional[str]]:
        payload = cls._unwrap_payload(payload)
        payload_type = payload.get("type", "")
        events: list[str] = []
        final_text: Optional[str] = None
        forge_event = extract_forge_event(payload)
        if forge_event:
            event_type = str(forge_event.get("type") or "").strip().lower()
            text = str(forge_event.get("text") or forge_event.get("message") or "").strip()
            event_payload = {k: v for k, v in forge_event.items() if k not in {"type", "text"}}
            events.append(encode_forge_event(event_type, text=text, **event_payload))
            if event_type == "result":
                final_text = text or (forge_event.get("result") if isinstance(forge_event.get("result"), str) else None)
            return events, final_text

        if payload_type in {"thread.started", "turn.started"}:
            if payload_type == "thread.started":
                events.append("⚙️ Инициализация сессии...")

        elif payload_type == "error":
            message = payload.get("message", "")
            if message:
                events.append(f"❌ {message}")

        elif payload_type in {"agent_message", "assistant_message"}:
            text = cls._extract_text(payload)
            if text:
                events.append(f"💬 {text}")
                final_text = text

        elif payload_type == "agent_message_delta":
            text = cls._extract_text(payload)
            if text:
                events.append(f"💬 {text}")

        elif payload_type in {"item.started", "item.completed"}:
            item = payload.get("item", {})
            item_type = item.get("type", "")
            if item_type == "command_execution":
                events.append("🔧 run_shell_command")
            elif item_type == "file_change":
                events.append("🔧 edit")
            elif item_type == "web_search":
                events.append("🔧 web_search")
            elif item_type == "todo_list":
                events.append("🔧 todo_write")
            elif item_type == "reasoning":
                text = cls._extract_text(item)
                if text:
                    events.append(f"🧠 {QwenProcessManager._truncate_thinking(text)}")
            elif item_type in {"assistant_message", "agent_message"}:
                text = cls._extract_text(item)
                if text:
                    events.append(f"💬 {text}")

        elif payload_type == "turn.completed":
            events.append("🏁 Завершено (success): 0ms")

        elif payload_type == "task_complete":
            events.append("🏁 Завершено (success): 0ms")
            final_text = cls._extract_text(payload) or final_text

        return events, final_text

    async def send_command(self, text: str, cwd: Path = None):
        async with self._command_lock:
            return await self._send_command_impl(text, cwd)

    async def _send_command_impl(self, text: str, cwd: Path = None):
        if not self._running:
            raise RuntimeError("Менеджер не запущен")

        work_dir = cwd or Path.cwd()
        with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False) as handle:
            output_file = Path(handle.name)

        args = [
            self.cli_path,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "-o",
            str(output_file),
        ]
        if self.model_name:
            args.extend(["-m", self.model_name])
        if self._session_active:
            args.extend(["resume", "--last", text])
        else:
            args.append(text)

        log.info("Запуск codex exec --json (model=%s) в %s", self.model_name or "default", work_dir)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
        )
        self._proc = proc
        log.info(f"Процесс запущен PID={proc.pid}, cwd={work_dir}")

        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
            except asyncio.TimeoutError:
                log.error(f"Таймаут {self.timeout}с — убиваю процесс PID={proc.pid}")
                proc.kill()
                await proc.wait()
                self._proc = None
                self.mark_failure(f"Timeout after {self.timeout}s")
                return -1

            if not line:
                break

            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("Невалидный JSON от codex: %s", raw[:200])
                continue

            events, final_text = self.parse_stream_payload(payload)
            for event in events:
                self._notify(event, raw)
            if final_text and self._final_result_callback:
                self._final_result_callback(final_text)

        stderr_parts: list[str] = []
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text_line = line.decode("utf-8", errors="replace").strip()
            if text_line:
                stderr_parts.append(text_line)
                log.info("Codex stderr: %s", text_line)

        await proc.wait()
        self._proc = None
        self._session_active = proc.returncode == 0

        try:
            if output_file.exists():
                final_text = output_file.read_text(encoding="utf-8", errors="replace").strip()
                if final_text and self._final_result_callback:
                    self._final_result_callback(final_text)
        finally:
            output_file.unlink(missing_ok=True)

        if proc.returncode != 0 and stderr_parts:
            self._notify(f"❌ {' '.join(stderr_parts)[:300]}", "")
            self.mark_failure(" ".join(stderr_parts))
        elif proc.returncode == 0:
            self.mark_success()
        else:
            self.mark_failure(f"Process exited with code {proc.returncode}")

        log.info("Процесс завершился с кодом %s", proc.returncode)
        return proc.returncode


class ClaudeProcessManager(BaseProcessManager):
    """
    Run Claude Code in non-interactive stream-json mode and normalize its
    events to the same emoji-oriented stream already used by the bot.
    """

    @staticmethod
    def _extract_text_from_message(payload: dict) -> str:
        direct_text = payload.get("text")
        if isinstance(direct_text, str) and direct_text.strip():
            return direct_text

        message = payload.get("message")
        if isinstance(message, dict):
            content = message.get("content", [])
            if isinstance(content, list):
                text_parts = [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
                ]
                if text_parts:
                    return "".join(text_parts)

        content = payload.get("content")
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
            ]
            if text_parts:
                return "".join(text_parts)
        return ""

    @classmethod
    def parse_stream_payload(cls, payload: dict) -> tuple[list[str], Optional[str]]:
        payload_type = payload.get("type", "")
        payload_subtype = payload.get("subtype", "")
        forge_event = extract_forge_event(payload)
        if forge_event:
            event_type = str(forge_event.get("type") or "").strip().lower()
            text = str(forge_event.get("text") or forge_event.get("message") or "").strip()
            event_payload = {k: v for k, v in forge_event.items() if k not in {"type", "text"}}
            event_line = encode_forge_event(event_type, text=text, **event_payload)
            final_text = text if event_type == "result" and text else None
            return [event_line], final_text

        if payload_type == "system" and payload_subtype == "init":
            return ["⚙️ Инициализация сессии..."], None

        if payload_type == "system" and payload_subtype == "api_retry":
            attempt = payload.get("attempt", "?")
            max_retries = payload.get("max_retries", "?")
            error = payload.get("error", "unknown")
            return [f"❌ Claude API retry {attempt}/{max_retries}: {error}"], None

        if payload_type == "system":
            status = payload.get("status") or payload.get("message")
            if isinstance(status, str) and status.strip():
                return [f"⚙️ {status}"], None

        if payload_type in {"assistant", "message", "text"}:
            text = cls._extract_text_from_message(payload)
            if text:
                return [f"💬 {text}"], text

        if payload_type in {"tool_use", "tool_call"}:
            tool_name = payload.get("name") or payload.get("tool_name") or "tool"
            return [f"🔧 {tool_name}"], None

        if payload_type == "tool_result":
            tool_name = payload.get("name") or payload.get("tool_name")
            if tool_name:
                return [f"🔧 Результат инструмента: {tool_name}"], None
            return ["🔧 Результат инструмента"], None

        if payload_type == "result":
            subtype = payload.get("subtype", "success")
            duration_ms = payload.get("duration_ms", 0)
            result_text = payload.get("result", "") or None
            usage = payload.get("usage") or {}
            input_tokens = usage.get("input_tokens") or usage.get("inputTokens") or 0
            output_tokens = usage.get("output_tokens") or usage.get("outputTokens") or 0
            events = [f"🏁 Завершено ({subtype}): {duration_ms}ms"]
            if input_tokens or output_tokens:
                events.append(f"🔢 {input_tokens},{output_tokens}")
            return events, result_text

        if payload_type == "error":
            message = payload.get("message", "")
            if message:
                return [f"❌ {message}"], None

        return QwenProcessManager.parse_stream_payload(payload)

    async def send_command(self, text: str, cwd: Path = None):
        async with self._command_lock:
            return await self._send_command_impl(text, cwd)

    async def _send_command_impl(self, text: str, cwd: Path = None):
        if not self._running:
            raise RuntimeError("Менеджер не запущен")

        work_dir = cwd or Path.cwd()
        args = [
            self.cli_path,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--permission-mode",
            "bypassPermissions",
        ]
        if self.model_name:
            args.extend(["--model", self.model_name])
        if self._session_active:
            args.append("-c")
        args.append(text)

        log.info("Запуск claude stream-json (model=%s) в %s", self.model_name or "default", work_dir)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
        )
        self._proc = proc
        log.info("Процесс запущен PID=%s, cwd=%s", proc.pid, work_dir)

        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self.timeout)
            except asyncio.TimeoutError:
                log.error("Таймаут %sс — убиваю процесс PID=%s", self.timeout, proc.pid)
                proc.kill()
                await proc.wait()
                self._proc = None
                self.mark_failure(f"Timeout after {self.timeout}s")
                return -1

            if not line:
                break

            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("Невалидный JSON от claude: %s", raw[:200])
                continue

            events, final_text = self.parse_stream_payload(payload)
            for event in events:
                self._notify(event, raw)
            if final_text and self._final_result_callback:
                self._final_result_callback(final_text)

        stderr_parts: list[str] = []
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text_line = line.decode("utf-8", errors="replace").strip()
            if text_line:
                stderr_parts.append(text_line)
                log.info("Claude stderr: %s", text_line)

        await proc.wait()
        self._proc = None
        self._session_active = proc.returncode == 0

        if proc.returncode != 0 and stderr_parts:
            self._notify(f"❌ {' '.join(stderr_parts)[:300]}", "")
            self.mark_failure(" ".join(stderr_parts))
        elif proc.returncode == 0:
            self.mark_success()
        else:
            self.mark_failure(f"Process exited with code {proc.returncode}")

        log.info("Процесс завершился с кодом %s", proc.returncode)
        return proc.returncode


def create_process_manager(
    provider: str,
    cli_path: str,
    on_output: Callable[[str], None],
    timeout: int = DEFAULT_TIMEOUT,
    model_name: str = "",
):
    normalized = normalize_provider_name(provider)
    if normalized == "codex":
        return CodexProcessManager(cli_path=cli_path, on_output=on_output, timeout=timeout, provider_name=normalized, model_name=model_name)
    if normalized == "claude":
        return ClaudeProcessManager(cli_path=cli_path, on_output=on_output, timeout=timeout, provider_name=normalized, model_name=model_name)
    return QwenProcessManager(cli_path=cli_path, on_output=on_output, timeout=timeout, provider_name=normalized, model_name=model_name)
