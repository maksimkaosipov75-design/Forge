import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.task_models import TaskRun
from desktop.gtk.app import (
    DRAWER_TRANSITION_MS,
    INSPECTOR_WIDTH,
    MESSAGE_TRANSITION_MS,
    SIDEBAR_WIDTH,
    format_recent_run_label,
    format_stream_event_for_chat,
    format_task_event_label,
    parse_unified_diff_preview,
    render_run_artifact_summary,
    render_run_chat_messages,
)


class DesktopGtkFormattingTests(unittest.TestCase):
    def test_desktop_uses_fixed_overlay_drawers_instead_of_resizable_panes(self):
        source = (Path(__file__).resolve().parents[1] / "desktop/gtk/app.py").read_text(encoding="utf-8")

        self.assertIn("Gtk.Overlay()", source)
        self.assertIn("set_resizable(True)", source)
        self.assertIn('notify::maximized', source)
        self.assertIn('notify::fullscreened', source)
        self.assertIn("set_hexpand(True)", source)
        self.assertIn("welcome-scroll", source)
        self.assertIn("app-shell", source)
        self.assertIn("left-rail", source)
        self.assertIn("right-drawer", source)
        self.assertIn("drawer-backdrop", source)
        self.assertIn("Gtk.GestureClick.new()", source)
        self.assertIn("_fit_drawer_sizes", source)
        self.assertIn("_hide_drawer_after_transition", source)
        self.assertEqual(DRAWER_TRANSITION_MS, 280)
        self.assertEqual(MESSAGE_TRANSITION_MS, 240)
        self.assertIn("_make_welcome_status", source)
        self.assertIn("_make_inspector_empty_state", source)
        self.assertIn("composer-busy", source)
        self.assertIn("brand-mark", source)
        self.assertIn("context-project-icon", source)
        self.assertIn("context-identity", source)
        self.assertIn("context-status", source)
        self.assertIn("context-runtime-card", source)
        self.assertIn("context-settings-button", source)
        self.assertIn("_make_icon_tile", source)
        self.assertIn("Gtk.FileChooserNative", source)
        self.assertIn("_set_working_directory", source)
        self.assertIn("_toggle_fullscreen", source)
        self.assertIn("_on_window_state_changed", source)
        self.assertIn("Gdk.KEY_F11", source)
        self.assertIn("provider_supports_thinking", source)
        self.assertIn("Gtk.ToggleButton(label=item)", source)
        self.assertIn("button.set_group(first_button)", source)
        self.assertIn("Search models by name, alias, or capability", source)
        self.assertIn("self._populate_model_results(provider, results, model_rows", source)
        self.assertIn("welcome-prompt-icon", source)
        self.assertIn("welcome-recents-action", source)
        self.assertIn('_show_inspector("tasks", reveal=False)', source)
        self.assertIn('_show_inspector("diff", reveal=False)', source)
        self.assertIn('_show_inspector("artifact", reveal=False)', source)
        self.assertNotIn("Gtk.Paned", source)
        self.assertEqual(SIDEBAR_WIDTH, 300)
        self.assertEqual(INSPECTOR_WIDTH, 380)

    def test_desktop_css_has_layered_surfaces_and_polished_popovers(self):
        css = (Path(__file__).resolve().parents[1] / "desktop/gtk/styles/forge.css").read_text(encoding="utf-8")

        self.assertIn("linear-gradient", css)
        self.assertIn("popover contents", css)
        self.assertIn(".forge-dialog", css)
        self.assertIn(".provider-dropdown", css)
        self.assertIn(".app-shell", css)
        self.assertIn(".left-rail", css)
        self.assertIn(".brand-mark", css)
        self.assertIn(".context-project-icon", css)
        self.assertIn(".context-identity", css)
        self.assertIn(".context-status", css)
        self.assertIn(".context-runtime-card", css)
        self.assertIn(".context-settings-button", css)
        self.assertIn(".welcome-scroll", css)
        self.assertIn(".welcome-prompt-icon", css)
        self.assertIn(".welcome-recents-action", css)
        self.assertIn(".sidebar-workspace-card", css)
        self.assertIn(".welcome-status", css)
        self.assertIn(".inspector-empty", css)
        self.assertIn(".composer-busy", css)
        self.assertIn("box-shadow", css)
        self.assertIn(".context-popover", css)
        self.assertIn(".model-thinking-row", css)
        self.assertIn(".model-thinking-segment", css)
        self.assertIn(".model-thinking-toggle:checked", css)

    def test_format_task_event_label_includes_status_provider_group_and_action(self):
        label = format_task_event_label(
            {
                "subtask_id": "ui-1",
                "title": "Build GTK shell",
                "provider": "claude",
                "status": "running",
                "parallel_group": 2,
                "text": "Editing app.py",
            }
        )

        self.assertEqual(label, "* Build GTK shell [claude] running - group 2 - Editing app.py")

    def test_format_task_event_label_uses_safe_defaults(self):
        label = format_task_event_label({"subtask_id": "task-1"})

        self.assertEqual(label, "* task-1 [mixed] running")

    def test_render_run_artifact_summary_includes_artifact_preview(self):
        with TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "run.md"
            artifact.write_text("# Result\n\nDone.", encoding="utf-8")
            run = TaskRun(
                run_id="run-1",
                prompt="demo",
                mode="single",
                status="success",
                provider_summary="qwen",
                artifact_file=str(artifact),
            )

            text = render_run_artifact_summary(run)

        self.assertIn("Run: run-1", text)
        self.assertIn("Providers: qwen", text)
        self.assertIn("Artifact preview:", text)
        self.assertIn("# Result", text)

    def test_render_run_artifact_summary_truncates_long_preview(self):
        run = TaskRun(
            run_id="run-2",
            prompt="demo",
            status="success",
            answer_text="abcdef",
        )

        text = render_run_artifact_summary(run, max_chars=3)

        self.assertIn("abc", text)

    def test_format_recent_run_label_compacts_prompt_and_metadata(self):
        run = TaskRun(
            run_id="run-3",
            prompt="Refactor the GTK desktop workspace into a calmer layout",
            status="success",
            provider_summary="claude",
        )

        title, meta = format_recent_run_label(run, max_prompt_chars=18)

        self.assertEqual(title, "Refactor the GTK...")
        self.assertEqual(meta, "success - claude")

    def test_render_run_chat_messages_restores_prompt_answer_and_files(self):
        run = TaskRun(
            run_id="run-4",
            prompt="Fix recents navigation",
            status="success",
            mode="single",
            provider_summary="codex",
            answer_text="Recents now opens the saved chat.",
        )

        messages = render_run_chat_messages(run)

        self.assertIn(("user", "Fix recents navigation"), messages)
        self.assertIn(("system", "SUCCESS - single - codex"), messages)
        self.assertIn(("assistant", "Recents now opens the saved chat."), messages)

    def test_render_run_chat_messages_keeps_artifact_markdown_out_of_chat(self):
        with TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "run.md"
            artifact.write_text("## Strategy\n\n- Provider: codex", encoding="utf-8")
            run = TaskRun(
                run_id="run-5",
                prompt="demo",
                status="failed",
                mode="single",
                provider_summary="codex",
                artifact_file=str(artifact),
            )

            messages = render_run_chat_messages(run)

        self.assertIn(("system", "Run details are available in the Artifact panel."), messages)
        self.assertNotIn(("assistant", "## Strategy\n\n- Provider: codex"), messages)

    def test_format_stream_event_for_chat_removes_raw_event_prefixes(self):
        self.assertEqual(format_stream_event_for_chat("⚙️ Initializing session..."), "Initializing session")
        self.assertEqual(format_stream_event_for_chat("🏁 Done (success): 0ms"), "Completed")

    def test_parse_unified_diff_preview_keeps_changed_code_lines(self):
        diff = "\n".join(
            [
                "diff --git a/app.py b/app.py",
                "index abc..def 100644",
                "--- a/app.py",
                "+++ b/app.py",
                "@@ -1,2 +1,2 @@",
                "-old = True",
                "+new = True",
                " context()",
            ]
        )

        preview = parse_unified_diff_preview(diff)

        self.assertIn(("diff-hunk", "@@ -1,2 +1,2 @@"), preview)
        self.assertIn(("diff-removed", "-old = True"), preview)
        self.assertIn(("diff-added", "+new = True"), preview)
        self.assertIn(("diff-context", " context()"), preview)
