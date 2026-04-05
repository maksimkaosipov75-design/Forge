import unittest

from process_manager import ClaudeProcessManager, CodexProcessManager, QwenProcessManager


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


if __name__ == "__main__":
    unittest.main()
