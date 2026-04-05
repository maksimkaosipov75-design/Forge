import unittest

from parser import LogParser


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
        parser.set_final_result("done")

        parser.clear_full_buffer()

        self.assertEqual(parser.full_buffer, [])
        self.assertEqual(parser.final_result, "")
        self.assertEqual(parser.state.tool_use_count, 0)
        self.assertEqual(parser.state.current_action, "Ожидание команды")


if __name__ == "__main__":
    unittest.main()
