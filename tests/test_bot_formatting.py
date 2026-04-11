"""Tests for bot.formatting helpers."""

import unittest
from pathlib import Path
from unittest.mock import patch

import bot.file_registry as reg
from bot.formatting import (
    build_file_preview_messages,
    build_interaction_buttons,
    build_plan_preview_buttons,
    build_task_buttons,
    chunk_code_sections,
    compose_html_messages,
    format_task_result_sections,
    truncate_text,
)


def _esc(text: str) -> str:
    """HTML-escape like the bot does."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class TruncateTextTests(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(truncate_text("hello", 20), "hello")

    def test_long_text_truncated_with_ellipsis(self):
        result = truncate_text("a" * 100, 20)
        self.assertTrue(result.endswith("…"))
        self.assertLessEqual(len(result), 21)

    def test_exact_length_unchanged(self):
        self.assertEqual(truncate_text("abc", 3), "abc")


class ComposeHtmlMessagesTests(unittest.TestCase):
    def test_short_sections_fit_in_one_message(self):
        sections = ["<b>Title</b>", "body text"]
        messages = compose_html_messages(sections)

        self.assertEqual(len(messages), 1)
        self.assertIn("<b>Title</b>", messages[0])

    def test_large_sections_split_into_multiple_messages(self):
        # Each section is ~1000 chars, max_len=3800 → expect 2 messages
        big = "x" * 1000
        sections = [big] * 5
        messages = compose_html_messages(sections, max_len=3800)

        self.assertGreater(len(messages), 1)
        for msg in messages:
            self.assertLessEqual(len(msg), 3900)  # small slack for separators


class ChunkCodeSectionsTests(unittest.TestCase):
    def test_plain_text_returned_as_single_chunk(self):
        chunks = chunk_code_sections("hello world", _esc)
        self.assertEqual(len(chunks), 1)
        self.assertIn("hello world", chunks[0])

    def test_code_fence_rendered_as_pre(self):
        text = "```python\nprint('hi')\n```"
        chunks = chunk_code_sections(text, _esc)
        combined = "\n".join(chunks)
        self.assertIn("<pre>", combined)
        self.assertIn("print", combined)


class FormatTaskResultSectionsTests(unittest.TestCase):
    def test_includes_success_header_when_files_present(self):
        sections = format_task_result_sections(
            Path("/tmp/project"),
            new_files=["/tmp/project/new.py"],
            changed_files=["/tmp/project/app.py"],
        )
        combined = "\n".join(sections)
        self.assertIn("✅", combined)

    def test_empty_files_returns_success_header_only(self):
        sections = format_task_result_sections(
            Path("/tmp/project"),
            new_files=[],
            changed_files=[],
        )
        self.assertEqual(sections, ["<b>✅ Задача выполнена</b>"])

    def test_new_files_listed(self):
        sections = format_task_result_sections(
            Path("/tmp"),
            new_files=["/tmp/alpha.py", "/tmp/beta.py"],
            changed_files=[],
        )
        combined = "\n".join(sections)
        self.assertIn("alpha.py", combined)
        self.assertIn("beta.py", combined)


class BuildTaskButtonsTests(unittest.TestCase):
    def setUp(self):
        reg._registry.clear()

    def test_returns_inline_keyboard(self):
        kb = build_task_buttons(Path("/tmp"), [], [], can_retry_failed=False)
        self.assertTrue(hasattr(kb, "inline_keyboard"))

    def test_file_buttons_callback_data_within_64_bytes(self):
        long_path = "/home/user/projects/very-long/src/module/submodule/file.py"
        kb = build_task_buttons(
            Path("/home/user/projects/very-long"),
            new_files=[long_path],
            changed_files=[],
        )
        for row in kb.inline_keyboard:
            for btn in row:
                if btn.callback_data and btn.callback_data.startswith("view_file:"):
                    self.assertLessEqual(
                        len(btn.callback_data.encode()), 64,
                        f"callback_data exceeds 64 bytes: {btn.callback_data!r}",
                    )

    def test_retry_failed_button_present_when_requested(self):
        kb = build_task_buttons(Path("/tmp"), [], [], can_retry_failed=True)
        all_data = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if btn.callback_data
        ]
        self.assertIn("retry_failed_subtask", all_data)

    def test_repeat_button_always_present(self):
        kb = build_task_buttons(Path("/tmp"), [], [])
        all_data = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if btn.callback_data
        ]
        self.assertIn("repeat_task", all_data)


class BuildPlanPreviewButtonsTests(unittest.TestCase):
    def test_run_edit_cancel_buttons_present(self):
        kb = build_plan_preview_buttons()
        all_data = {
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if btn.callback_data
        }
        self.assertIn("plan_run", all_data)
        self.assertIn("plan_edit", all_data)
        self.assertIn("plan_cancel", all_data)


class BuildInteractionButtonsTests(unittest.TestCase):
    def test_approval_kind_has_yes_no(self):
        kb = build_interaction_buttons("approval")
        all_data = {
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if btn.callback_data
        }
        self.assertIn("interaction:yes", all_data)
        self.assertIn("interaction:no", all_data)

    def test_question_kind_has_skip(self):
        kb = build_interaction_buttons("question")
        all_data = {
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if btn.callback_data
        }
        self.assertIn("interaction:skip", all_data)


class BuildFilePreviewMessagesTests(unittest.TestCase):
    def test_small_file_included_inline(self):
        content = "def hello():\n    pass\n"
        messages = build_file_preview_messages(Path("hello.py"), content, _esc)
        self.assertTrue(messages)
        combined = "\n".join(messages)
        self.assertIn("hello.py", combined)

    def test_large_file_truncated(self):
        content = "x" * 100_000
        messages = build_file_preview_messages(Path("big.txt"), content, _esc)
        combined = "\n".join(messages)
        # Should still produce output, not crash
        self.assertTrue(combined)


if __name__ == "__main__":
    unittest.main()
