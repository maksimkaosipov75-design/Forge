import unittest

from core.event_protocol import encode_forge_event
from core.parser import LogParser


class LogParserTests(unittest.TestCase):
    def test_get_full_response_prefers_final_result(self):
        parser = LogParser()
        parser.feed("💬 partial")
        parser.set_final_result("final answer")

        self.assertEqual(parser.get_full_response(), "final answer")

    def test_feed_updates_status_for_tool_and_done(self):
        parser = LogParser()

        parser.feed("🔧 read_file")
        self.assertTrue(parser.state.is_busy)
        self.assertEqual(parser.state.tool_use_count, 1)

        parser.feed("🏁 Завершено (success): 123ms")
        self.assertFalse(parser.state.is_busy)
        self.assertIn("Задача завершена", parser.state.current_action)

    def test_clear_full_buffer_resets_transient_state(self):
        parser = LogParser()
        parser.feed("💬 hello")
        parser.feed("🔧 edit")
        parser.feed("🔢 12,34")
        parser.set_final_result("done")

        parser.clear_full_buffer()

        self.assertEqual(parser.full_buffer, [])
        self.assertEqual(parser.final_result, "")
        self.assertEqual(parser.state.tool_use_count, 0)
        self.assertEqual(parser.state.current_action, "Ожидание команды")
        self.assertEqual(parser.state.last_input_tokens, 0)
        self.assertEqual(parser.state.last_output_tokens, 0)

    def test_feed_tracks_token_usage(self):
        parser = LogParser()

        parser.feed("🔢 12,34")
        parser.feed("🔢 5,6")

        self.assertEqual(parser.get_token_usage(), (5, 6, 17, 40))

    def test_feed_parses_forge_question_event(self):
        parser = LogParser()

        parser.feed(
            encode_forge_event(
                "question",
                text="Need API key?",
                title="Authorization",
                options=[{"id": "key", "label": "API key"}],
            )
        )

        self.assertEqual(parser.state.events[-1].category.value, "question")
        self.assertEqual(parser.state.events[-1].payload["title"], "Authorization")

    def test_get_actionable_line_formats_forge_question_event(self):
        parser = LogParser()

        line = encode_forge_event("question", text="Need API key?", title="Authorization")

        self.assertEqual(parser.get_actionable_line(line), "❓ Authorization")


if __name__ == "__main__":
    unittest.main()
