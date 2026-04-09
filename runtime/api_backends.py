import asyncio
import json
import logging
from pathlib import Path
from typing import Callable, Optional
from urllib import error, request

from provider_status import FailureReason, ProviderHealth, classify_failure_text


log = logging.getLogger(__name__)


class BaseApiBackend:
    def __init__(
        self,
        provider_name: str,
        on_output: Callable[[str], None],
        model_name: str = "",
        timeout: int = 120,
    ):
        self.provider_name = provider_name
        self.on_output = on_output
        self.model_name = model_name
        self.timeout = timeout
        self.health = ProviderHealth(provider=provider_name)
        self._running = False
        self._stream_callback: Optional[Callable[[str], None]] = None
        self._final_result_callback: Optional[Callable[[str], None]] = None

    async def start(self):
        self._running = True
        log.info("%s backend initialized", self.provider_name)

    async def stop(self):
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def set_stream_callback(self, callback: Optional[Callable[[str], None]]):
        self._stream_callback = callback

    def set_final_result_callback(self, callback: Optional[Callable[[str], None]]):
        self._final_result_callback = callback

    def _notify(self, event: str):
        failure = classify_failure_text(event)
        if failure:
            self.health.register_failure(failure)
        if self._stream_callback:
            self._stream_callback(event)
        self.on_output(event)

    def mark_success(self):
        self.health.register_success()

    def mark_failure(self, text: str):
        failure = classify_failure_text(text) or FailureReason(
            kind="unknown",
            message=text or "Unknown failure",
            source_text=text or "",
        )
        self.health.register_failure(failure)


class OpenRouterExecutionBackend(BaseApiBackend):
    @staticmethod
    def _usage_event(payload: dict) -> str:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return ""

        input_tokens = (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("inputTokens")
            or 0
        )
        output_tokens = (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("outputTokens")
            or 0
        )
        if isinstance(input_tokens, int) and isinstance(output_tokens, int) and (input_tokens or output_tokens):
            return f"🔢 {input_tokens},{output_tokens}"
        return ""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        on_output: Callable[[str], None],
        model_name: str = "",
        timeout: int = 120,
        app_name: str = "Forge",
    ):
        super().__init__(
            provider_name="openrouter",
            on_output=on_output,
            model_name=model_name,
            timeout=timeout,
        )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.app_name = app_name

    @staticmethod
    def parse_sse_line(raw: str) -> tuple[list[str], str]:
        line = raw.strip()
        if not line.startswith("data:"):
            return [], ""

        payload_text = line[5:].strip()
        if not payload_text or payload_text == "[DONE]":
            return [], ""

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return [], ""

        events: list[str] = []
        text_delta = ""

        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            message = error_payload.get("message", "")
            if message:
                events.append(f"❌ {message}")
            return events, text_delta

        usage_event = OpenRouterExecutionBackend._usage_event(payload)
        if usage_event:
            events.append(usage_event)

        choices = payload.get("choices")
        if not isinstance(choices, list):
            return events, text_delta

        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_delta += content

        if text_delta:
            events.append(f"💬 {text_delta}")
        return events, text_delta

    def _build_request(self, prompt: str, model_name: str) -> request.Request:
        payload = {
            "model": model_name,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [
                {"role": "user", "content": prompt},
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            method="POST",
        )
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("HTTP-Referer", "https://github.com/maksimkaosipov75-design/Forge")
        req.add_header("X-Title", self.app_name)
        return req

    def _send_request_sync(self, prompt: str, model_name: str) -> tuple[list[str], str]:
        if not self.api_key.strip():
            raise RuntimeError("OPENROUTER_API_KEY is not configured")

        req = self._build_request(prompt, model_name)
        aggregated_parts: list[str] = []
        emitted_events: list[str] = []

        with request.urlopen(req, timeout=self.timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace")
                events, text_delta = self.parse_sse_line(line)
                emitted_events.extend(events)
                if text_delta:
                    aggregated_parts.append(text_delta)

        return emitted_events, "".join(aggregated_parts).strip()

    async def send_command(self, text: str, cwd: Path = None):
        if not self._running:
            raise RuntimeError("Менеджер не запущен")

        model_name = self.model_name.strip()
        if not model_name:
            raise RuntimeError("OpenRouter model is not configured")

        try:
            loop = asyncio.get_running_loop()
            events, final_text = await loop.run_in_executor(None, self._send_request_sync, text, model_name)
            for event in events:
                self._notify(event)
            if final_text and self._final_result_callback:
                self._final_result_callback(final_text)
            self._notify("🏁 Завершено (success): 0ms")
            self.mark_success()
            return 0
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = f"HTTP {exc.code}: {detail or exc.reason}"
            self._notify(f"❌ {message[:300]}")
            self.mark_failure(message)
            return exc.code or -1
        except error.URLError as exc:
            message = f"Network error: {exc.reason}"
            self._notify(f"❌ {message}")
            self.mark_failure(message)
            return -1
        except Exception as exc:
            message = str(exc)
            self._notify(f"❌ {message[:300]}")
            self.mark_failure(message)
            return -1
