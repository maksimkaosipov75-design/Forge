import unittest

from core.event_protocol import decode_forge_event
from core.process_manager import ClaudeProcessManager, CodexProcessManager, QwenProcessManager


class ProcessManagerPayloadParsingTests(unittest.TestCase):
    def test_parse_stream_event_text_delta(self):
        payload = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello"},
            },
        }

        events, final_text = QwenProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["💬 hello"])
        self.assertIsNone(final_text)

    def test_parse_stream_event_tool_start(self):
        payload = {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "read_file"},
            },
        }

        events, final_text = QwenProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["🔧 read_file"])
        self.assertIsNone(final_text)

    def test_parse_result_returns_event_and_final_text(self):
        payload = {
            "type": "result",
            "subtype": "success",
            "duration_ms": 42,
            "result": "final answer",
        }

        events, final_text = QwenProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["🏁 Завершено (success): 42ms"])
        self.assertEqual(final_text, "final answer")

    def test_parse_assistant_thinking_is_truncated(self):
        payload = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "a" * 120},
                ]
            },
        }

        events, _ = QwenProcessManager.parse_stream_payload(payload)

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].startswith("🧠 "))
        self.assertLessEqual(len(events[0]), 102)

    def test_parse_codex_agent_message(self):
        payload = {"type": "agent_message", "message": "hello"}

        events, final_text = CodexProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["💬 hello"])
        self.assertEqual(final_text, "hello")

    def test_parse_codex_item_started_command_execution(self):
        payload = {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "ls"},
        }

        events, final_text = CodexProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["🔧 run_shell_command"])
        self.assertIsNone(final_text)

    def test_parse_codex_error(self):
        payload = {"type": "error", "message": "Reconnecting..."}

        events, _ = CodexProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["❌ Reconnecting..."])

    def test_parse_claude_system_init(self):
        payload = {"type": "system", "subtype": "init"}

        events, final_text = ClaudeProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["⚙️ Инициализация сессии..."])
        self.assertIsNone(final_text)

    def test_parse_claude_api_retry(self):
        payload = {
            "type": "system",
            "subtype": "api_retry",
            "attempt": 2,
            "max_retries": 10,
            "error": "unknown",
        }

        events, final_text = ClaudeProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["❌ Claude API retry 2/10: unknown"])
        self.assertIsNone(final_text)

    def test_parse_claude_text_message(self):
        payload = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hello from claude"},
                ]
            },
        }

        events, final_text = ClaudeProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["💬 hello from claude"])
        self.assertEqual(final_text, "hello from claude")

    def test_parse_claude_tool_use(self):
        payload = {"type": "tool_use", "name": "read_file"}

        events, final_text = ClaudeProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["🔧 read_file"])
        self.assertIsNone(final_text)

    def test_parse_claude_tool_result(self):
        payload = {"type": "tool_result", "name": "read_file"}

        events, final_text = ClaudeProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["🔧 Результат инструмента: read_file"])
        self.assertIsNone(final_text)

    def test_parse_claude_result_includes_tokens(self):
        payload = {
            "type": "result",
            "subtype": "success",
            "duration_ms": 75,
            "result": "done",
            "usage": {"input_tokens": 12, "output_tokens": 34},
        }

        events, final_text = ClaudeProcessManager.parse_stream_payload(payload)

        self.assertEqual(events, ["🏁 Завершено (success): 75ms", "🔢 12,34"])
        self.assertEqual(final_text, "done")

    def test_parse_qwen_forge_event_passthrough(self):
        payload = {
            "type": "assistant",
            "forge_event": {
                "type": "question",
                "text": "Need API key?",
                "title": "Authorization",
            },
        }

        events, final_text = QwenProcessManager.parse_stream_payload(payload)

        self.assertIsNone(final_text)
        decoded = decode_forge_event(events[0])
        self.assertEqual(decoded["type"], "question")
        self.assertEqual(decoded["title"], "Authorization")


if __name__ == "__main__":
    unittest.main()
