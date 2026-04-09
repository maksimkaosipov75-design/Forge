import unittest
import json

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


if __name__ == "__main__":
    unittest.main()
