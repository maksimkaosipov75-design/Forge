import unittest

from cli.thinking import append_thinking_chunk, extract_thinking_chunk, render_thinking_text


class CliThinkingTests(unittest.TestCase):
    def test_extract_thinking_chunk_preserves_leading_spaces(self):
        self.assertEqual(extract_thinking_chunk("🧠  world"), " world")

    def test_append_thinking_chunk_reconstructs_streamed_sentence(self):
        buffer = ""
        buffer = append_thinking_chunk(buffer, "🧠 Н")
        buffer = append_thinking_chunk(buffer, "🧠 ужно сп")
        buffer = append_thinking_chunk(buffer, "🧠 ланировать")
        self.assertEqual(buffer, "Нужно спланировать")

    def test_render_thinking_text_compact_truncates(self):
        rendered = render_thinking_text("x" * 220, "compact", rich=False)
        self.assertTrue(rendered.startswith("  Thinking: "))
        self.assertTrue(rendered.endswith("…"))

    def test_render_thinking_text_rich_escapes_markup(self):
        rendered = render_thinking_text("[debug]", "full", rich=True)
        self.assertIn("\\[debug]", rendered)

    def test_render_thinking_text_off_returns_none(self):
        self.assertIsNone(render_thinking_text("hidden", "off", rich=False))


if __name__ == "__main__":
    unittest.main()
