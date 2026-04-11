import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Callable, Optional
from urllib import error, request

from core.event_protocol import encode_forge_event, extract_forge_event
from core.provider_status import FailureReason, ProviderHealth, classify_failure_text


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
        self._cancel_event = threading.Event()
        self._active_response = None  # urllib response object for the running request

    async def start(self):
        self._running = True
        log.info("%s backend initialized", self.provider_name)

    async def stop(self):
        self._running = False
        self._cancel_event.set()
        # Close the active HTTP response from another thread to unblock the reader.
        resp = self._active_response
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

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
    def _extract_error_message(detail: str, fallback: str = "") -> str:
        text = (detail or fallback or "").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text

        if isinstance(payload, dict):
            error_payload = payload.get("error")
            if isinstance(error_payload, dict):
                message = error_payload.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
        return text

    @staticmethod
    def _friendly_http_error_message(code: int, detail: str, reason: str, model_name: str) -> str:
        raw_message = OpenRouterExecutionBackend._extract_error_message(detail, reason) or "Unknown OpenRouter error"
        selected_model = model_name.strip() or "current model"
        is_free_model = selected_model.endswith(":free") or selected_model == "openrouter/free"

        if code == 401:
            return (
                "OpenRouter rejected the API key. Re-enter the key with /auth openrouter "
                "or rotate it in OpenRouter if it may have been revoked. "
                f"Details: {raw_message}"
            )

        if code == 429:
            hint = (
                f"The selected free model/router ({selected_model}) is currently rate-limited or unavailable. "
                "Try another free model, wait a bit, or use a paid model."
                if is_free_model
                else f"The selected model ({selected_model}) is currently rate-limited. Try again shortly or switch models."
            )
            return f"OpenRouter accepted the key, but request limits blocked this call. {hint} Details: {raw_message}"

        return f"HTTP {code}: {raw_message}"

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
        # Set by the executor before each send_command call.
        self.thinking_enabled: bool = False
        self.conversation_history: list[dict] = []

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
        forge_event = extract_forge_event(payload)
        if forge_event:
            event_type = str(forge_event.get("type") or "").strip().lower()
            text = str(forge_event.get("text") or forge_event.get("message") or "").strip()
            event_payload = {k: v for k, v in forge_event.items() if k not in {"type", "text"}}
            events.append(encode_forge_event(event_type, text=text, **event_payload))
            if event_type == "result" and text:
                text_delta = text
            return events, text_delta

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
            # Some models (DeepSeek-R1, Qwen3, etc.) return reasoning as a
            # top-level "reasoning" or "reasoning_content" field in the delta.
            for reasoning_key in ("reasoning", "reasoning_content"):
                reasoning_chunk = delta.get(reasoning_key)
                if isinstance(reasoning_chunk, str) and reasoning_chunk:
                    events.append(encode_forge_event("thinking", text=reasoning_chunk))
                    break
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_delta += content
            elif isinstance(content, list):
                # Extended delta format used by Claude (thinking blocks, text blocks).
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype in ("thinking", "reasoning"):
                        thinking_text = (block.get("thinking") or block.get("reasoning") or "").strip()
                        if thinking_text:
                            events.append(encode_forge_event("thinking", text=thinking_text))
                    elif btype in ("text", ""):
                        part = block.get("text", "")
                        if part:
                            text_delta += part

        if text_delta:
            events.append(f"💬 {text_delta}")
        return events, text_delta

    def _build_messages(self, prompt: str) -> list[dict]:
        """Build the messages array from conversation history + current prompt."""
        messages: list[dict] = []
        for entry in self.conversation_history:
            if entry.get("role") and entry.get("content"):
                messages.append({"role": entry["role"], "content": entry["content"]})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _build_request(self, prompt: str, model_name: str) -> request.Request:
        messages = self._build_messages(prompt)
        payload: dict = {
            "model": model_name,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": messages,
        }
        if self.thinking_enabled:
            # OpenRouter standard reasoning parameter — works for all
            # reasoning-capable models (Qwen3, DeepSeek-R1, Claude, etc.)
            payload["reasoning"] = {"effort": "high"}
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

    def _stream_sync(
        self,
        prompt: str,
        model_name: str,
        loop: asyncio.AbstractEventLoop,
    ) -> str:
        """
        Runs in a thread-pool executor.

        Emits SSE events in real-time via *loop.call_soon_threadsafe* so the
        UI status-bar and thinking-buffer update while the HTTP response is
        still streaming — identical to how CLI providers work.

        Returns the aggregated plain-text answer for the final-result callback.
        """
        if not self.api_key.strip():
            raise RuntimeError("OPENROUTER_API_KEY is not configured")

        self._cancel_event.clear()
        req = self._build_request(prompt, model_name)
        aggregated: list[str] = []

        with request.urlopen(req, timeout=self.timeout) as response:
            self._active_response = response
            try:
                for raw_line in response:
                    if self._cancel_event.is_set():
                        break
                    line = raw_line.decode("utf-8", errors="replace")
                    events, text_delta = self.parse_sse_line(line)
                    for event in events:
                        # Schedule _notify on the event-loop thread so that the
                        # stream_event_callback (which updates Rich Live) runs
                        # in the correct asyncio context.
                        loop.call_soon_threadsafe(self._notify, event)
                    if text_delta:
                        aggregated.append(text_delta)
            finally:
                self._active_response = None

        return "".join(aggregated).strip()

    async def send_command(self, text: str, cwd: Path = None):
        if not self._running:
            raise RuntimeError("Manager not started")

        model_name = self.model_name.strip()
        if not model_name:
            raise RuntimeError("OpenRouter model is not configured")

        loop = asyncio.get_running_loop()
        try:
            final_text = await loop.run_in_executor(
                None, self._stream_sync, text, model_name, loop
            )
            if final_text and self._final_result_callback:
                self._final_result_callback(final_text)
            self._notify("🏁 Done (success): 0ms")
            self.mark_success()
            return 0
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = self._friendly_http_error_message(
                exc.code,
                detail,
                str(exc.reason or ""),
                model_name,
            )
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
