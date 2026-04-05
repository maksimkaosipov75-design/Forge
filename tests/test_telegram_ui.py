import unittest
from pathlib import Path

from telegram_ui import (
    build_task_buttons,
    build_file_preview_messages,
    chunk_code_sections,
    compose_html_messages,
    format_task_result_sections,
)


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class TelegramUiTests(unittest.TestCase):
    def test_format_task_result_sections_includes_files(self):
        sections = format_task_result_sections(
            Path("/tmp/project"),
            new_files=["/tmp/project/new.py"],
            changed_files=["/tmp/project/app.py"],
        )

        self.assertIn("<b>✅ Задача выполнена</b>", sections)
        self.assertIn("• <code>new.py</code>", sections)
        self.assertIn("• <code>app.py</code>", sections)

    def test_chunk_code_sections_wraps_pre_blocks(self):
        text = "line 1\n" * 1200

        chunks = chunk_code_sections(text, escape_html, max_len=200)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith("<pre>") for chunk in chunks))

    def test_compose_html_messages_splits_large_sections(self):
        sections = ["a" * 2000, "b" * 2000, "c" * 2000]

        messages = compose_html_messages(sections, max_len=3000)

        self.assertGreater(len(messages), 1)

    def test_build_file_preview_messages_generates_continuations(self):
        messages = build_file_preview_messages(Path("sample.py"), "print('x')\n" * 800, escape_html)

        self.assertGreater(len(messages), 1)
        self.assertIn("<b>sample.py</b>", messages[0])

    def test_build_task_buttons_can_include_retry_failed(self):
        keyboard = build_task_buttons(
            Path("/tmp/project"),
            new_files=[],
            changed_files=[],
            can_retry_failed=True,
        )

        callback_data = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn("retry_failed_subtask", callback_data)


if __name__ == "__main__":
    unittest.main()
