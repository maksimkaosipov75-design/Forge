import json
import unittest

from core.process_manager import AntigravityProcessManager, create_process_manager
from core.providers import get_provider_definition, is_api_provider, is_supported_provider

# Captured from the real CLI: `agy --model gemini-3.5-flash-low
# --output-format stream-json "--print=say OK"` on a region-locked account.
# It is the only envelope that could be observed, so it is worth keeping exactly
# as it came back.
REAL_ERROR_LINE = (
    '{"event":"result","result":{"conversation_id":"","status":"ERROR","response":"",'
    '"error":"Eligibility check failed: Your current account is not eligible for '
    'Antigravity, because it is not currently available in your location.",'
    '"duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,'
    '"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}}'
)


class ProviderRegistrationTests(unittest.TestCase):
    def test_the_provider_exists_and_is_a_cli_one(self):
        self.assertTrue(is_supported_provider("antigravity"))
        self.assertFalse(is_api_provider("antigravity"))

    def test_the_binary_is_agy_not_the_provider_name(self):
        """The product is Antigravity; the command is agy. Getting this wrong
        makes readiness checks look for a command that does not exist."""
        self.assertEqual(get_provider_definition("antigravity").default_cli_path, "agy")

    def test_the_factory_builds_the_right_manager(self):
        manager = create_process_manager(
            provider="antigravity", cli_path="agy", on_output=lambda _line: None
        )
        self.assertIsInstance(manager, AntigravityProcessManager)


class StreamParsingTests(unittest.TestCase):
    def parse(self, payload):
        return AntigravityProcessManager.parse_stream_payload(payload)

    def test_the_real_error_line_is_surfaced(self):
        events, final = self.parse(json.loads(REAL_ERROR_LINE))
        self.assertIsNone(final)
        self.assertEqual(len(events), 1)
        self.assertIn("not eligible", events[0])

    def test_an_error_result_never_becomes_a_final_answer(self):
        """status ERROR carries an empty response; returning it would present
        failure as a blank success."""
        _, final = self.parse(json.loads(REAL_ERROR_LINE))
        self.assertIsNone(final)

    def test_a_successful_result_yields_the_response_and_usage(self):
        events, final = self.parse({
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "OK",
                "duration_seconds": 1.5,
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        })
        self.assertEqual(final, "OK")
        self.assertIn("1500ms", events[0])
        self.assertIn("12,3", events[1])

    def test_events_are_tagged_by_event_not_type(self):
        """The Claude CLI uses {"type": ...}; agy uses {"event": name, name: {...}}.
        Reading it the Claude way silently produces nothing."""
        events, _ = self.parse({"event": "result", "result": {"status": "SUCCESS", "response": "hi"}})
        self.assertTrue(events)

    def test_text_bearing_events_are_shown_even_though_unnamed(self):
        """The events of a successful turn were never observed, so anything
        carrying text is shown rather than dropped."""
        events, _ = self.parse({"event": "assistant", "assistant": {"text": "thinking out loud"}})
        self.assertEqual(events, ["💬 thinking out loud"])

    def test_tool_events_are_named(self):
        events, _ = self.parse({"event": "tool", "tool": {"tool_name": "read_file"}})
        self.assertEqual(events, ["🔧 read_file"])

    def test_an_unknown_event_is_ignored_rather_than_crashing(self):
        events, final = self.parse({"event": "something_new", "something_new": {"a": 1}})
        self.assertEqual(events, [])
        self.assertIsNone(final)

    def test_a_flat_payload_still_parses(self):
        """Not every event is guaranteed to nest its fields under its own name."""
        events, final = self.parse({"event": "result", "status": "SUCCESS", "response": "flat"})
        self.assertEqual(final, "flat")


if __name__ == "__main__":
    unittest.main()
