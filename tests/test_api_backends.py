import unittest
import asyncio
import json
import io
from urllib import error, request

from core.event_protocol import decode_forge_event
from runtime.api_backends import LocalLLMExecutionBackend, OpenRouterExecutionBackend


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

    def test_local_build_request_omits_auth_and_stream_usage_by_default(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:11434/v1",
            on_output=lambda _line: None,
            model_name="qwen2.5-coder:7b",
        )

        req = backend._build_request("hello", "qwen2.5-coder:7b")
        payload = json.loads(req.data.decode("utf-8"))

        self.assertNotIn("stream_options", payload)
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        headers = dict(req.header_items())
        lowered_headers = {key.casefold(): value for key, value in headers.items()}
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("http-referer", lowered_headers)
        self.assertNotIn("x-title", lowered_headers)
        self.assertEqual(backend.provider_name, "local")
        self.assertFalse(backend.prefer_streaming)

    def test_local_qwen3_build_request_adds_no_think_by_default(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:1234/v1",
            on_output=lambda _line: None,
            model_name="qwen3-coder-30b-a3b-instruct",
        )

        req = backend._build_request("Привет", "qwen3-coder-30b-a3b-instruct")
        payload = json.loads(req.data.decode("utf-8"))

        self.assertEqual(payload["messages"][-1]["content"], "Привет\n\n/no_think")

    def test_local_qwen3_no_think_can_be_disabled(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:1234/v1",
            on_output=lambda _line: None,
            model_name="qwen3-coder-30b-a3b-instruct",
            disable_thinking=False,
        )

        req = backend._build_request("Привет", "qwen3-coder-30b-a3b-instruct")
        payload = json.loads(req.data.decode("utf-8"))

        self.assertEqual(payload["messages"][-1]["content"], "Привет")

    def test_local_chat_only_folds_context_into_user_message(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:1234/v1",
            on_output=lambda _line: None,
            model_name="llama3.1:8b",
        )
        backend.project_context = "Answer directly."

        req = backend._build_request("hello", "llama3.1:8b")
        payload = json.loads(req.data.decode("utf-8"))

        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][0]["content"], "Answer directly.\n\nUser request:\nhello")

    def test_local_think_filter_handles_split_tags(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:1234/v1",
            on_output=lambda _line: None,
            model_name="deepseek-r1",
        )
        backend._reset_output_filter()

        self.assertEqual(backend._filter_output_text("before <thi"), "before ")
        self.assertEqual(backend._filter_output_text("nk>hidden</think> after"), " after")

    def test_local_detects_degenerate_token_noise(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:1234/v1",
            on_output=lambda _line: None,
            model_name="local-model",
        )

        self.assertTrue(
            backend._looks_like_degenerate_text(
                "a320p499920192020202020202222222222222222222222222t22222k2"
            )
        )
        self.assertFalse(backend._looks_like_degenerate_text("Я умею отвечать на вопросы и помогать с кодом."))

    def test_local_compatibility_retry_uses_plain_deterministic_request(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:1234/v1",
            on_output=lambda _line: None,
            model_name="local-model",
        )
        backend._compatibility_retry = True

        req = backend._build_request("hello", "local-model", stream=False)
        payload = json.loads(req.data.decode("utf-8"))

        self.assertFalse(payload["stream"])
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["top_p"], 1)
        self.assertEqual(payload["max_tokens"], 512)

    def test_local_buffers_stream_text_and_retries_degenerate_result(self):
        events: list[str] = []
        final_results: list[str] = []
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:1234/v1",
            on_output=events.append,
            model_name="local-model",
        )
        backend._running = True
        backend.set_final_result_callback(final_results.append)
        calls = {"retry": 0}

        def fake_stream(_messages, _model_name, _loop):
            return "a320p499920192020202020202222222222222222222222222t22222k2", [], "stop"

        def fake_non_stream(_messages, _model_name, _loop):
            calls["retry"] += 1
            backend._notify("💬 clean answer")
            return "clean answer", [], "stop"

        backend._stream_iteration_sync = fake_stream  # type: ignore[method-assign]
        backend._non_stream_iteration_sync = fake_non_stream  # type: ignore[method-assign]

        async def run():
            return await backend.send_command("hello")

        exit_code = asyncio.run(run())

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls["retry"], 1)
        self.assertNotIn("a320p4999", "\n".join(events))
        self.assertIn("clean answer", "\n".join(events))
        self.assertEqual(final_results, ["clean answer"])

    def test_local_unreadable_stream_falls_back_to_non_streaming(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:1234/v1",
            on_output=lambda _line: None,
            model_name="local-model",
            prefer_streaming=True,
        )
        calls: list[bool] = []
        original_urlopen = request.urlopen

        class FakeResponse:
            def __init__(self, *, lines=None, body: bytes = b""):
                self.lines = lines or []
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return iter(self.lines)

            def read(self):
                return self.body

        def fake_urlopen(req, timeout=None):
            payload = json.loads(req.data.decode("utf-8"))
            calls.append(payload["stream"])
            if payload["stream"]:
                return FakeResponse(lines=[b'{"choices":[{"delta":{"content":"ignored"}}]}\n'])
            return FakeResponse(
                body=b'{"choices":[{"message":{"content":"fallback ok"},"finish_reason":"stop"}]}'
            )

        request.urlopen = fake_urlopen
        loop = asyncio.new_event_loop()
        try:
            text, tool_calls, finish_reason = backend._stream_iteration_sync(
                [{"role": "user", "content": "hello"}],
                "local-model",
                loop,
            )
        finally:
            request.urlopen = original_urlopen
            loop.close()

        self.assertEqual(calls, [True, False])
        self.assertEqual(text, "fallback ok")
        self.assertEqual(tool_calls, [])
        self.assertEqual(finish_reason, "stop")

    def test_local_prefers_non_stream_when_streaming_disabled(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:1234/v1",
            on_output=lambda _line: None,
            model_name="local-model",
            prefer_streaming=False,
        )
        backend._running = True
        calls = {"non_stream": 0}

        def fake_non_stream(_messages, _model_name, _loop):
            calls["non_stream"] += 1
            return "stable answer", [], "stop"

        backend._non_stream_iteration_sync = fake_non_stream  # type: ignore[method-assign]

        async def run():
            return await backend.send_command("hello")

        exit_code = asyncio.run(run())

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls["non_stream"], 1)

    def test_local_build_request_can_enable_tools_explicitly(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:11434/v1",
            on_output=lambda _line: None,
            model_name="qwen2.5-coder:7b",
            tools_enabled=True,
        )

        req = backend._build_request("hello", "qwen2.5-coder:7b")
        payload = json.loads(req.data.decode("utf-8"))

        self.assertIn("tools", payload)
        self.assertEqual(payload["tool_choice"], "auto")

    def test_local_backend_has_long_cold_start_timeout_and_loading_message(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:11434/v1",
            on_output=lambda _line: None,
            model_name="qwen2.5-coder:7b",
            timeout=300,
        )

        self.assertGreaterEqual(backend.startup_timeout, 300)
        self.assertIn("Loading local model qwen2.5-coder:7b", backend._loading_message("qwen2.5-coder:7b"))

    def test_local_friendly_404_mentions_base_url_and_model(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:11434/v1",
            on_output=lambda _line: None,
            model_name="missing-model",
        )

        message = backend._friendly_error_message(
            404,
            '{"error":{"message":"model not found"}}',
            "Not Found",
            "missing-model",
        )

        self.assertIn("Local LLM endpoint or model was not found", message)
        self.assertIn("LOCAL_LLM_BASE_URL", message)
        self.assertIn("missing-model", message)

    def test_local_retries_without_tools_when_model_rejects_tool_calling(self):
        events: list[str] = []
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:11434/v1",
            on_output=events.append,
            model_name="llama3.1:latest",
            tools_enabled=True,
        )
        backend._running = True
        calls = {"count": 0}

        def fake_stream(_messages, _model_name, _loop):
            calls["count"] += 1
            if calls["count"] == 1:
                self.assertTrue(backend.tools_enabled)
                raise error.HTTPError(
                    "http://127.0.0.1:11434/v1/chat/completions",
                    400,
                    "Bad Request",
                    hdrs=None,
                    fp=io.BytesIO(b'{"error":{"message":"llama3.1:latest does not support tools"}}'),
                )
            self.assertFalse(backend.tools_enabled)
            return "chat response", [], "stop"

        backend._stream_iteration_sync = fake_stream  # type: ignore[method-assign]

        async def run():
            return await backend.send_command("hello")

        exit_code = __import__("asyncio").run(run())

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls["count"], 2)
        self.assertTrue(any("chat-only mode" in event for event in events))

    def test_local_tool_fallback_does_not_persist_across_commands(self):
        backend = LocalLLMExecutionBackend(
            base_url="http://127.0.0.1:11434/v1",
            on_output=lambda _line: None,
            model_name="llama3.1:latest",
            tools_enabled=True,
        )
        backend._running = True
        calls = {"count": 0}

        def fake_stream(_messages, _model_name, _loop):
            calls["count"] += 1
            if calls["count"] == 1:
                raise error.HTTPError(
                    "http://127.0.0.1:11434/v1/chat/completions",
                    400,
                    "Bad Request",
                    hdrs=None,
                    fp=io.BytesIO(b'{"error":{"message":"model does not support tools"}}'),
                )
            return "chat response", [], "stop"

        backend._stream_iteration_sync = fake_stream  # type: ignore[method-assign]

        async def run_once():
            return await backend.send_command("hello")

        exit_code = asyncio.run(run_once())
        self.assertEqual(exit_code, 0)
        self.assertTrue(backend.tools_enabled, "tools setting should reset to configured value")

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

        def raise_http_error(_messages, _model_name, _loop):
            raise error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                429,
                "Too Many Requests",
                hdrs=None,
                fp=None,
            )

        backend._stream_iteration_sync = raise_http_error  # type: ignore[method-assign]

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

    def test_prune_messages_no_op_within_budget(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi", "tool_calls": []},
            {"role": "tool", "tool_call_id": "1", "content": "result"},
        ]
        pruned = OpenRouterExecutionBackend._prune_messages(msgs, budget=100_000)
        self.assertEqual(len(pruned), 4)

    def test_prune_messages_truncates_old_tool_results(self):
        big = "x" * 10_000
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": big},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
            {"role": "tool", "tool_call_id": "2", "content": big},
        ]
        pruned = OpenRouterExecutionBackend._prune_messages(msgs, budget=5_000)
        # All messages preserved but tool results truncated
        tool_msgs = [m for m in pruned if m.get("role") == "tool"]
        self.assertTrue(all(len(m["content"]) <= 500 for m in tool_msgs))

    def test_prune_messages_preserves_system_and_user(self):
        big = "x" * 50_000
        msgs = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "original task"},
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "tool", "tool_call_id": "1", "content": big},
        ]
        pruned = OpenRouterExecutionBackend._prune_messages(msgs, budget=1_000)
        roles = [m["role"] for m in pruned]
        self.assertIn("system", roles)
        self.assertIn("user", roles)


if __name__ == "__main__":
    unittest.main()
