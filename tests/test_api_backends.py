import unittest
import json
from urllib import error

from event_protocol import decode_forge_event
from runtime.api_backends import OpenRouterExecutionBackend


class OpenRouterExecutionBackendTests(unittest.TestCase):
    def test_parse_sse_line_extracts_text_delta(self):
        raw = 'data: {"choices":[{"delta":{"content":"hello"}}]}'

        events, text_delta = OpenRouterExecutionBackend.parse_sse_line(raw)

        self.assertEqual(events, ["💬 hello"])
        self.assertEqual(text_delta, "hello")

    def test_parse_sse_line_ignores_done_marker(self):
        events, text_delta = OpenRouterExecutionBackend.parse_sse_line("data: [DONE]")

        self.assertEqual(events, [])
        self.assertEqual(text_delta, "")

    def test_parse_sse_line_extracts_error(self):
        raw = 'data: {"error":{"message":"rate limited"}}'

        events, text_delta = OpenRouterExecutionBackend.parse_sse_line(raw)

        self.assertEqual(events, ["❌ rate limited"])
        self.assertEqual(text_delta, "")

    def test_parse_sse_line_extracts_usage_event(self):
        raw = 'data: {"usage":{"prompt_tokens":12,"completion_tokens":34}}'

        events, text_delta = OpenRouterExecutionBackend.parse_sse_line(raw)

        self.assertEqual(events, ["🔢 12,34"])
        self.assertEqual(text_delta, "")

    def test_build_request_enables_stream_usage(self):
        backend = OpenRouterExecutionBackend(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            on_output=lambda _line: None,
            model_name="qwen/qwen3-coder:free",
        )

        req = backend._build_request("hello", "qwen/qwen3-coder:free")
        payload = json.loads(req.data.decode("utf-8"))

        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_friendly_http_error_message_for_401(self):
        message = OpenRouterExecutionBackend._friendly_http_error_message(
            401,
            '{"error":{"message":"Missing Authentication header"}}',
            "Unauthorized",
            "qwen/qwen3-coder:free",
        )

        self.assertIn("rejected the API key", message)
        self.assertIn("/auth openrouter", message)
        self.assertIn("Missing Authentication header", message)

    def test_friendly_http_error_message_for_429_free_model(self):
        message = OpenRouterExecutionBackend._friendly_http_error_message(
            429,
            '{"error":{"message":"Rate limit exceeded"}}',
            "Too Many Requests",
            "qwen/qwen3-coder:free",
        )

        self.assertIn("accepted the key", message)
        self.assertIn("free model/router", message)
        self.assertIn("qwen/qwen3-coder:free", message)
        self.assertIn("Rate limit exceeded", message)

    def test_send_command_reports_friendly_http_429_message(self):
        events: list[str] = []
        backend = OpenRouterExecutionBackend(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            on_output=events.append,
            model_name="qwen/qwen3-coder:free",
        )

        backend._running = True

        def raise_http_error(_prompt: str, _model_name: str, _loop):
            raise error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                429,
                "Too Many Requests",
                hdrs=None,
                fp=None,
            )

        backend._stream_sync = raise_http_error  # type: ignore[method-assign]

        async def run():
            return await backend.send_command("hello")

        exit_code = __import__("asyncio").run(run())

        self.assertEqual(exit_code, 429)
        self.assertTrue(events)
        self.assertIn("accepted the key", events[0])
        self.assertIn("free model/router", events[0])

    def test_parse_sse_line_supports_forge_event_payload(self):
        raw = 'data: {"forge_event":{"type":"approval","text":"Allow shell?","title":"Shell access"}}'

        events, text_delta = OpenRouterExecutionBackend.parse_sse_line(raw)

        decoded = decode_forge_event(events[0])
        self.assertEqual(decoded["type"], "approval")
        self.assertEqual(decoded["title"], "Shell access")
        self.assertEqual(text_delta, "")


if __name__ == "__main__":
    unittest.main()
