from __future__ import annotations

import asyncio
import logging
import sys
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Coroutine

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from core.event_protocol import decode_forge_event
from cli.session_actions import get_thinking_mode, set_thinking_mode
from core.providers import get_provider_definition, normalize_provider_name, provider_default_model, provider_supports_thinking
from runtime import RuntimeContainer


APP_ID = "io.github.forge.Desktop"
log = logging.getLogger(__name__)
PROVIDER_ACCENTS = {
    "qwen": "#9b6cff",
    "codex": "#4c8dff",
    "claude": "#d97745",
    "openrouter": "#3f9f68",
    "local": "#e04f5f",
}
PROVIDER_CLASS_NAMES = ("provider-qwen", "provider-codex", "provider-claude", "provider-openrouter", "provider-local")
SIDEBAR_WIDTH = 300
INSPECTOR_WIDTH = 380
DRAWER_TRANSITION_MS = 280
MESSAGE_TRANSITION_MS = 240


@dataclass
class DesktopRunState:
    thread: threading.Thread | None = None
    active_provider: str = ""
    busy: bool = False


def _css_path() -> Path:
    return Path(__file__).resolve().parent / "styles" / "forge.css"


def _append_text(buffer: Gtk.TextBuffer, text: str, tag_name: str | None = None) -> None:
    end_iter = buffer.get_end_iter()
    if tag_name:
        buffer.insert_with_tags_by_name(end_iter, text, tag_name)
    else:
        buffer.insert(end_iter, text)
    buffer.insert(buffer.get_end_iter(), "\n")


def format_task_event_label(event: dict) -> str:
    status = str(event.get("status") or "running").strip()
    provider = str(event.get("provider") or "mixed").strip()
    title = str(event.get("title") or event.get("subtask_id") or "task").strip()
    action = str(event.get("text") or "").strip()
    group = event.get("parallel_group")
    icon = {
        "pending": ".",
        "running": "*",
        "success": "OK",
        "failed": "!",
        "partial": "!",
        "skipped": "-",
        "reused": "~",
    }.get(status, "*")
    suffix = f" - {action}" if action else ""
    group_text = f" - group {group}" if group not in {None, "", 0, "0"} else ""
    return f"{icon} {title} [{provider}] {status}{group_text}{suffix}"


def render_run_artifact_summary(task_run, max_chars: int = 2400) -> str:
    lines = [
        f"Run: {task_run.run_id}",
        f"Status: {task_run.status}",
        f"Mode: {task_run.mode}",
    ]
    if task_run.provider_summary:
        lines.append(f"Providers: {task_run.provider_summary}")
    if task_run.model_summary:
        lines.append(f"Models: {task_run.model_summary}")
    if task_run.duration_ms:
        lines.append(f"Duration: {task_run.duration_text}")
    if task_run.total_input_tokens or task_run.total_output_tokens:
        lines.append(f"Tokens: {task_run.total_input_tokens} in / {task_run.total_output_tokens} out")
    if task_run.artifact_file:
        lines.append(f"Artifact: {task_run.artifact_file}")
    if task_run.touched_files:
        lines.extend(["", "Files:", *[f"- {path}" for path in task_run.touched_files]])

    artifact_text = ""
    if task_run.artifact_file:
        try:
            path = Path(task_run.artifact_file)
            if path.is_file():
                artifact_text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            artifact_text = ""
    if artifact_text:
        if len(artifact_text) > max_chars:
            artifact_text = artifact_text[:max_chars].rstrip() + "\n\n... truncated ..."
        lines.extend(["", "Artifact preview:", "", artifact_text])
    elif task_run.answer_text.strip():
        lines.extend(["", "Answer:", "", task_run.answer_text.strip()[:max_chars]])
    elif task_run.error_text.strip():
        lines.extend(["", "Error:", "", task_run.error_text.strip()[:max_chars]])
    return "\n".join(lines)


def render_run_chat_messages(task_run, max_chars: int = 6000) -> list[tuple[str, str]]:
    prompt = " ".join((task_run.prompt or "").split()) or "Untitled request"
    provider = task_run.provider_summary or "mixed"
    status = task_run.status or "pending"
    meta = f"{status.upper()} - {task_run.mode} - {provider}"
    messages = [
        ("user", prompt),
        ("system", meta),
    ]

    answer_parts: list[tuple[str, str]] = []
    if task_run.answer_text.strip():
        answer_parts.append(("assistant", task_run.answer_text.strip()))
    if task_run.synthesis_answer.strip():
        answer_parts.append(("assistant", task_run.synthesis_answer.strip()))
    if task_run.review_answer.strip():
        answer_parts.append(("assistant", f"Review:\n{task_run.review_answer.strip()}"))
    if task_run.error_text.strip():
        answer_parts.append(("error", task_run.error_text.strip()))

    if not answer_parts and task_run.artifact_file:
        answer_parts.append(("system", "Run details are available in the Artifact panel."))

    if not answer_parts:
        answer_parts.append(("system", "This run did not save assistant output. Open Artifact for metadata."))

    for tag, text in answer_parts:
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n\n... truncated ..."
        messages.append((tag, text))

    if task_run.touched_files:
        count = len(task_run.touched_files)
        label = "file" if count == 1 else "files"
        messages.append(("system", f"{count} touched {label}. Open Files for details."))
    return messages


def format_stream_event_for_chat(line: str) -> str:
    text = " ".join((line or "").split())
    prefixes = {
        "⚙️": "",
        "❌": "",
        "💬": "",
        "🏁": "",
        "🔧": "Tool: ",
        "🧠": "Thinking: ",
        "📊": "",
        "📂": "",
        "✏️": "",
    }
    for prefix, replacement in prefixes.items():
        if text.startswith(prefix):
            text = replacement + text[len(prefix):].strip()
            break
    replacements = {
        "Initializing session...": "Initializing session",
        "Done (success): 0ms": "Completed",
        "Reading additional input from stdin...": "Waiting for CLI input",
    }
    return replacements.get(text, text)


def parse_unified_diff_preview(diff_text: str, max_lines: int = 80) -> list[tuple[str, str]]:
    preview: list[tuple[str, str]] = []
    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("diff --git", "index ", "--- ", "+++ ")):
            continue
        css_class = "diff-context"
        if raw_line.startswith("@@"):
            css_class = "diff-hunk"
        elif raw_line.startswith("+"):
            css_class = "diff-added"
        elif raw_line.startswith("-"):
            css_class = "diff-removed"
        elif raw_line.startswith(" "):
            css_class = "diff-context"
        else:
            continue
        preview.append((css_class, raw_line))
        if len(preview) >= max_lines:
            preview.append(("diff-context", "..."))
            break
    return preview


def format_recent_run_label(task_run, max_prompt_chars: int = 34) -> tuple[str, str]:
    prompt = " ".join((task_run.prompt or "").split()) or task_run.mode or "Untitled"
    if len(prompt) > max_prompt_chars:
        prompt = prompt[: max_prompt_chars - 1].rstrip() + "..."
    meta_parts = [task_run.status or "pending", task_run.provider_summary or "mixed"]
    if task_run.duration_ms:
        meta_parts.append(task_run.duration_text)
    return prompt, " - ".join(meta_parts)


class ForgeDesktopWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, container: RuntimeContainer, chat_id: int = 0):
        super().__init__(application=app)
        self.container = container
        self.chat_id = chat_id
        self.session = container.get_session(chat_id)
        self.run_state = DesktopRunState()
        self.set_title("Forge Desktop")
        self.set_default_size(1440, 900)
        self.set_resizable(True)
        self.connect("notify::maximized", self._on_window_state_changed)
        self.connect("notify::fullscreened", self._on_window_state_changed)

        self.provider_accent_widgets: list[Gtk.Widget] = []
        self.provider_buttons: dict[str, Gtk.ToggleButton] = {}
        self.provider_nav_rows: dict[str, Gtk.Button] = {}
        self.panel_views: dict[int, Gtk.TextView] = {}
        self.task_rows: list[Gtk.Widget] = []
        self.active_task_rows: dict[str, dict[str, Gtk.Widget]] = {}
        self.recent_row_runs: dict[int, object] = {}
        self.active_recent_run_id = ""
        self.composer_mode = "write"
        self.mode_buttons: dict[str, Gtk.Button] = {}
        self.nav_buttons: dict[str, Gtk.Button] = {}
        self.welcome_recent_box: Gtk.Box | None = None
        self._suppress_provider_change = False
        self._fullscreened = False

        self._build_ui()
        self._refresh_session_list()
        self._refresh_provider_state()
        self._refresh_run_history()
        self._refresh_context_bar()
        self._append_system_intro()

    @staticmethod
    def _run_coro_in_thread(coro: Coroutine[Any, Any, Any]) -> Any:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def _build_ui(self) -> None:
        root = Adw.ToolbarView()
        self.set_content(root)

        header = Adw.HeaderBar()
        header.add_css_class("flat")
        title = Gtk.Label(label="Forge")
        title.add_css_class("window-title")
        header.set_title_widget(title)
        root.add_top_bar(header)

        self.sidebar_toggle_button = Gtk.Button.new_from_icon_name("sidebar-show-symbolic")
        self.sidebar_toggle_button.add_css_class("toolbar-icon")
        self.sidebar_toggle_button.set_tooltip_text("Toggle sidebar")
        self.sidebar_toggle_button.connect("clicked", self._on_toggle_sidebar_clicked)
        header.pack_start(self.sidebar_toggle_button)

        self.provider_menu = Gtk.DropDown.new_from_strings(list(self.container.provider_paths.keys()))
        self.provider_menu.add_css_class("provider-dropdown")
        self.provider_menu.set_tooltip_text("Provider")
        self.provider_menu.set_size_request(148, -1)
        self.provider_menu.connect("notify::selected", self._on_provider_dropdown_changed)

        self.model_label = Gtk.Label(label="")
        self.model_label.add_css_class("toolbar-pill")
        self.model_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.model_label.set_max_width_chars(28)
        self.model_label.set_size_request(220, -1)

        self.model_button = Gtk.Button(label="Model")
        self.model_button.add_css_class("composer-action")
        self.model_button.set_tooltip_text("Set the model for the current provider")
        self.model_button.connect("clicked", self._on_model_clicked)

        self.workspace_status = Gtk.Label(label="Local workspace")
        self.workspace_status.add_css_class("composer-permission")
        self.workspace_status.set_tooltip_text("Runs use the current project directory and provider settings.")

        self.auth_button = Gtk.Button.new_from_icon_name("dialog-password-symbolic")
        self.auth_button.add_css_class("composer-icon")
        self.auth_button.set_tooltip_text("Configure OpenRouter API key")
        self.auth_button.connect("clicked", self._on_auth_clicked)

        self.open_artifact_button = Gtk.Button.new_from_icon_name("document-open-symbolic")
        self.open_artifact_button.add_css_class("toolbar-icon")
        self.open_artifact_button.set_tooltip_text("Show the latest run artifact")
        self.open_artifact_button.connect("clicked", self._on_open_artifact_clicked)
        self.open_artifact_button.set_sensitive(False)

        self.run_plan_button = Gtk.Button()
        self.run_plan_button.add_css_class("mode-chip")
        self.run_plan_button.set_child(self._make_button_content("media-playback-start-symbolic", "Run plan"))
        self.run_plan_button.set_tooltip_text("Run the latest plan as an orchestrated task")
        self.run_plan_button.connect("clicked", self._on_run_plan_clicked)
        self.run_plan_button.set_sensitive(False)

        self.plan_button = Gtk.Button()
        self.plan_button.add_css_class("mode-chip")
        self.plan_button.set_child(self._make_button_content("view-list-symbolic", "Plan"))
        self.plan_button.set_tooltip_text("Create an agent plan without executing it")
        self.plan_button.connect("clicked", self._on_plan_clicked)

        self.stop_button = Gtk.Button.new_from_icon_name("process-stop-symbolic")
        self.stop_button.add_css_class("toolbar-icon")
        self.stop_button.set_tooltip_text("Stop active provider runtime")
        self.stop_button.connect("clicked", self._on_stop_clicked)
        self.stop_button.set_sensitive(False)

        self.status_label = Gtk.Label(label="Ready")
        self.status_label.add_css_class("status-soft")
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_max_width_chars(18)
        header.pack_end(self.status_label)

        self.inspector_toggle_button = Gtk.Button.new_from_icon_name("sidebar-show-right-symbolic")
        self.inspector_toggle_button.add_css_class("toolbar-icon")
        self.inspector_toggle_button.set_tooltip_text("Toggle workspace panel")
        self.inspector_toggle_button.connect("clicked", self._on_toggle_inspector_clicked)
        header.pack_end(self.inspector_toggle_button)

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

        app_shell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        app_shell.add_css_class("app-shell")
        app_shell.set_hexpand(True)
        app_shell.set_vexpand(True)
        root.set_content(app_shell)

        self.sidebar_revealer = Gtk.Revealer()
        self.sidebar_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.sidebar_revealer.set_transition_duration(DRAWER_TRANSITION_MS)
        self.sidebar_revealer.set_halign(Gtk.Align.START)
        self.sidebar_revealer.set_valign(Gtk.Align.FILL)
        self.sidebar_revealer.set_vexpand(True)
        self.sidebar_revealer.add_css_class("left-rail")
        self.sidebar_revealer.set_reveal_child(True)
        self.sidebar_revealer.set_visible(True)
        self.sidebar_revealer.set_child(self._build_sidebar())
        app_shell.append(self.sidebar_revealer)
        app_shell.append(self._build_workspace())
        self._set_sidebar_visible(True)
        self._set_inspector_visible(False)

    def _build_drawer_backdrop(self) -> Gtk.Widget:
        backdrop = Gtk.Box()
        backdrop.add_css_class("drawer-backdrop")
        backdrop.set_hexpand(True)
        backdrop.set_vexpand(True)
        backdrop.set_halign(Gtk.Align.FILL)
        backdrop.set_valign(Gtk.Align.FILL)
        backdrop.set_visible(False)
        backdrop.set_can_target(False)
        return backdrop

    def _build_sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar.add_css_class("sidebar")
        sidebar.set_size_request(SIDEBAR_WIDTH, -1)

        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        brand.add_css_class("brand-block")
        mark = Gtk.Label(label="F")
        mark.add_css_class("brand-mark")
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(label="Forge")
        title.add_css_class("brand-title")
        title.set_xalign(0)
        subtitle = Gtk.Label(label="Multi-provider agent workspace")
        subtitle.add_css_class("brand-subtitle")
        subtitle.set_xalign(0)
        copy.append(title)
        copy.append(subtitle)
        brand.append(mark)
        brand.append(copy)
        sidebar.append(brand)

        nav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        new_button = self._make_sidebar_nav_button("user-home-symbolic", "Home")
        new_button.connect("clicked", self._on_new_session_clicked)
        nav_box.append(new_button)
        search_button = self._make_sidebar_nav_button("folder-symbolic", "Projects")
        search_button.connect("clicked", self._on_search_nav_clicked)
        nav_box.append(search_button)
        providers_button = self._make_sidebar_nav_button("application-x-addon-symbolic", "Providers")
        providers_button.connect("clicked", self._on_providers_nav_clicked)
        nav_box.append(providers_button)
        automations_button = self._make_sidebar_nav_button("media-playback-start-symbolic", "Runs")
        automations_button.connect("clicked", self._on_automations_nav_clicked)
        nav_box.append(automations_button)
        settings_button = self._make_sidebar_nav_button("emblem-system-symbolic", "Settings")
        settings_button.connect("clicked", self._on_settings_nav_clicked)
        nav_box.append(settings_button)
        self.nav_buttons.update(
            {
                "search": search_button,
                "providers": providers_button,
                "automations": automations_button,
                "settings": settings_button,
            }
        )
        sidebar.append(nav_box)

        sessions_label = Gtk.Label(label="Recent work")
        sessions_label.add_css_class("section-label")
        sessions_label.set_xalign(0)
        sidebar.append(sessions_label)

        self.sessions_list = Gtk.ListBox()
        self.sessions_list.add_css_class("navigation-list")
        self.sessions_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.sessions_list.set_activate_on_single_click(True)
        self.sessions_list.connect("row-selected", self._on_recent_row_selected)
        self.sessions_list.connect("row-activated", self._on_recent_row_activated)
        recents_scroll = Gtk.ScrolledWindow()
        recents_scroll.add_css_class("sidebar-scroll")
        recents_scroll.set_child(self.sessions_list)
        recents_scroll.set_vexpand(True)
        recents_scroll.set_min_content_height(150)
        sidebar.append(recents_scroll)

        providers_label = Gtk.Label(label="Providers")
        providers_label.add_css_class("section-label")
        providers_label.set_xalign(0)
        sidebar.append(providers_label)

        providers_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for provider_name in self.container.provider_paths:
            definition = get_provider_definition(provider_name)
            row = Gtk.ToggleButton()
            label = Gtk.Label(label=definition.label)
            label.set_xalign(0)
            label.set_hexpand(True)
            row.set_child(label)
            row.add_css_class("provider-button")
            row.add_css_class(f"provider-{provider_name}")
            row.set_tooltip_text(f"{definition.transport}: {', '.join(definition.specialties)}")
            row.connect("clicked", self._on_provider_button_clicked, provider_name)
            providers_box.append(row)
            self.provider_buttons[provider_name] = row
        sidebar.append(providers_box)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)

        workspace_card = Gtk.Button()
        workspace_card.add_css_class("sidebar-workspace-card")
        workspace_card.set_tooltip_text("Change working directory")
        workspace_card.connect("clicked", self._on_change_workspace_clicked)
        workspace_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        workspace_icon = Gtk.Image.new_from_icon_name("computer-symbolic")
        workspace_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        workspace_title = Gtk.Label(label="Local workspace")
        workspace_title.add_css_class("sidebar-workspace-title")
        workspace_title.set_xalign(0)
        self.sidebar_workspace_meta = Gtk.Label(label=str(self.session.file_mgr.get_working_dir()))
        self.sidebar_workspace_meta.add_css_class("sidebar-workspace-meta")
        self.sidebar_workspace_meta.set_xalign(0)
        self.sidebar_workspace_meta.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        workspace_copy.append(workspace_title)
        workspace_copy.append(self.sidebar_workspace_meta)
        workspace_row.append(workspace_icon)
        workspace_row.append(workspace_copy)
        workspace_card.set_child(workspace_row)
        sidebar.append(workspace_card)

        return sidebar

    def _make_sidebar_nav_button(self, icon_name: str, label_text: str) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("sidebar-nav")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        label = Gtk.Label(label=label_text)
        label.set_xalign(0)
        label.set_hexpand(True)
        row.append(icon)
        row.append(label)
        button.set_child(row)
        return button

    def _make_button_content(self, icon_name: str, label_text: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_halign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        label = Gtk.Label(label=label_text)
        label.set_xalign(0)
        row.append(icon)
        row.append(label)
        return row

    def _make_icon_tile(self, icon_name: str, css_class: str) -> Gtk.Widget:
        tile = Gtk.CenterBox()
        tile.add_css_class(css_class)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        tile.set_center_widget(icon)
        return tile

    def _build_workspace(self) -> Gtk.Widget:
        workspace_overlay = Gtk.Overlay()
        workspace_overlay.add_css_class("workspace-overlay")
        workspace_overlay.set_hexpand(True)
        workspace_overlay.set_vexpand(True)

        workspace = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.workspace_root = workspace
        self.provider_accent_widgets.append(workspace)
        workspace.add_css_class("workspace")
        workspace.set_hexpand(True)
        workspace.set_vexpand(True)
        workspace_overlay.set_child(workspace)

        chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        chat_box.add_css_class("chat-pane")
        chat_box.set_hexpand(True)
        chat_box.set_vexpand(True)
        workspace.append(chat_box)

        context_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.context_bar = context_bar
        self.provider_accent_widgets.append(context_bar)
        context_bar.add_css_class("context-bar")
        self.run_spinner = Gtk.Spinner()
        self.run_spinner.add_css_class("run-spinner")
        self.run_spinner.set_visible(False)
        context_bar.append(self.run_spinner)
        context_project = Gtk.Button()
        context_project.add_css_class("context-project")
        context_project.set_tooltip_text("Change working directory")
        context_project.connect("clicked", self._on_change_workspace_clicked)
        context_project.set_hexpand(True)
        context_project_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        context_project_row.set_hexpand(True)
        context_icon = self._make_icon_tile("folder-symbolic", "context-project-icon")
        context_identity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        context_identity.add_css_class("context-identity")
        context_identity.set_hexpand(True)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.context_title = Gtk.Label(label="")
        self.context_title.add_css_class("context-title")
        self.context_title.set_xalign(0)
        self.context_title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.context_title.set_max_width_chars(48)
        self.context_title.set_hexpand(True)
        self.context_status = Gtk.Label(label="")
        self.context_status.add_css_class("context-status")
        self.context_meta = Gtk.Label(label="")
        self.context_meta.add_css_class("context-meta")
        self.context_meta.set_xalign(0)
        self.context_meta.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.context_meta.set_max_width_chars(70)
        title_row.append(self.context_title)
        title_row.append(self.context_status)
        context_identity.append(title_row)
        context_identity.append(self.context_meta)
        context_project_row.append(context_icon)
        context_project_row.append(context_identity)
        context_project.set_child(context_project_row)
        context_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        context_actions.add_css_class("context-actions")
        provider_card = Gtk.Button()
        provider_card.add_css_class("context-runtime-card")
        provider_card.add_css_class("context-provider-card")
        provider_card.set_tooltip_text("Change provider")
        provider_card.connect("clicked", self._on_providers_nav_clicked)
        provider_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        provider_label = Gtk.Label(label="Provider")
        provider_label.add_css_class("context-runtime-title")
        provider_label.set_xalign(0)
        provider_value_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.context_provider_value = Gtk.Label(label="")
        self.context_provider_value.add_css_class("context-runtime-value")
        self.context_provider_value.set_xalign(0)
        self.context_provider_value.set_hexpand(True)
        provider_chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        provider_chevron.add_css_class("context-runtime-chevron")
        provider_value_row.append(self.context_provider_value)
        provider_value_row.append(provider_chevron)
        provider_content.append(provider_label)
        provider_content.append(provider_value_row)
        provider_card.set_child(provider_content)
        model_card = Gtk.Button()
        model_card.add_css_class("context-runtime-card")
        model_card.add_css_class("context-model-card")
        model_card.set_tooltip_text("Search or edit model")
        model_card.connect("clicked", self._on_model_clicked)
        model_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        model_label = Gtk.Label(label="Model")
        model_label.add_css_class("context-runtime-title")
        model_label.set_xalign(0)
        model_value_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.context_model_value = Gtk.Label(label="")
        self.context_model_value.add_css_class("context-runtime-value")
        self.context_model_value.set_xalign(0)
        self.context_model_value.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.context_model_value.set_max_width_chars(20)
        self.context_model_value.set_hexpand(True)
        model_chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        model_chevron.add_css_class("context-runtime-chevron")
        model_value_row.append(self.context_model_value)
        model_value_row.append(model_chevron)
        model_content.append(model_label)
        model_content.append(model_value_row)
        model_card.set_child(model_content)
        context_actions.append(provider_card)
        context_actions.append(model_card)
        tune = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        tune.add_css_class("toolbar-icon")
        tune.add_css_class("context-settings-button")
        tune.set_tooltip_text("Workspace settings")
        tune.connect("clicked", self._on_settings_nav_clicked)
        context_actions.append(tune)
        context_bar.append(context_project)
        context_bar.append(context_actions)
        chat_box.append(context_bar)

        self.chat_stack = Gtk.Stack()
        self.chat_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.chat_stack.set_transition_duration(220)
        self.chat_stack.set_hexpand(True)
        self.chat_stack.set_vexpand(True)

        empty_page = self._build_empty_page()
        self.chat_stack.add_named(empty_page, "welcome")

        self.transcript_buffer = Gtk.TextBuffer()
        self.transcript_buffer.create_tag(
            "system",
            foreground="#8f8a82",
            left_margin=24,
            right_margin=24,
            pixels_above_lines=8,
            pixels_below_lines=5,
        )
        self.transcript_buffer.create_tag(
            "user",
            foreground="#f3eee7",
            weight=Pango.Weight.BOLD,
            left_margin=150,
            right_margin=24,
            pixels_above_lines=12,
            pixels_below_lines=8,
        )
        self.transcript_buffer.create_tag(
            "assistant",
            foreground="#ded8cf",
            left_margin=24,
            right_margin=120,
            pixels_above_lines=12,
            pixels_below_lines=8,
        )
        self.transcript_buffer.create_tag(
            "event",
            foreground="#b9a99a",
            left_margin=42,
            right_margin=42,
            pixels_above_lines=4,
            pixels_below_lines=4,
        )
        self.transcript_buffer.create_tag(
            "error",
            foreground="#ff8a8a",
            weight=Pango.Weight.BOLD,
            left_margin=24,
            right_margin=120,
            pixels_above_lines=10,
            pixels_below_lines=8,
        )

        scroll = Gtk.ScrolledWindow()
        scroll.add_css_class("message-scroll")
        scroll.set_vexpand(True)
        self.message_scroller = scroll
        self.message_feed = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.message_feed.add_css_class("message-feed")
        self.message_feed.set_valign(Gtk.Align.START)
        scroll.set_child(self.message_feed)
        self.chat_stack.add_named(scroll, "transcript")
        self.chat_stack.add_named(self._build_search_page(), "search")
        self.chat_stack.add_named(self._build_providers_page(), "providers")
        self.chat_stack.add_named(self._build_automations_page(), "automations")
        self.chat_stack.add_named(self._build_settings_page(), "settings")
        self.chat_stack.set_visible_child_name("welcome")
        chat_box.append(self.chat_stack)

        composer_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        composer_outer.add_css_class("composer-outer")
        composer_outer.set_hexpand(True)
        composer_outer.set_vexpand(False)
        composer_outer.set_halign(Gtk.Align.FILL)
        composer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.composer_box = composer
        self.provider_accent_widgets.append(composer)
        composer.add_css_class("composer")
        composer.set_hexpand(True)
        composer.set_size_request(360, -1)
        self.prompt_hint = Gtk.Label(label="Write with project context")
        self.prompt_hint.add_css_class("prompt-hint")
        self.prompt_hint.set_xalign(0)
        composer.append(self.prompt_hint)
        self.prompt_entry = Gtk.TextView()
        self.prompt_entry.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.prompt_entry.set_hexpand(True)
        self.prompt_entry.set_vexpand(False)
        self.prompt_entry.set_size_request(-1, 68)
        self.prompt_entry.add_css_class("prompt")
        prompt_focus = Gtk.EventControllerFocus()
        prompt_focus.connect("enter", self._on_prompt_focus_entered)
        self.prompt_entry.add_controller(prompt_focus)
        composer.append(self.prompt_entry)

        mode_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mode_strip.add_css_class("mode-strip")
        write_button = Gtk.Button()
        write_button.add_css_class("mode-chip")
        write_button.add_css_class("mode-chip-active")
        write_button.set_child(self._make_button_content("document-edit-symbolic", "Write"))
        write_button.connect("clicked", self._on_write_mode_clicked)
        code_button = Gtk.Button()
        code_button.add_css_class("mode-chip")
        code_button.set_child(self._make_button_content("applications-engineering-symbolic", "Code"))
        code_button.connect("clicked", self._on_code_mode_clicked)
        review_button = Gtk.Button()
        review_button.add_css_class("mode-chip")
        review_button.set_child(self._make_button_content("system-search-symbolic", "Review"))
        review_button.connect("clicked", self._on_review_mode_clicked)
        self.mode_buttons = {
            "write": write_button,
            "plan": self.plan_button,
            "code": code_button,
            "review": review_button,
        }
        mode_strip.append(write_button)
        mode_strip.append(self.plan_button)
        mode_strip.append(code_button)
        mode_strip.append(review_button)
        mode_strip.append(self.run_plan_button)
        composer.append(mode_strip)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.add_css_class("composer-controls")
        add_button = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add_button.add_css_class("composer-icon")
        add_button.set_tooltip_text("Add context")
        add_button.connect("clicked", self._on_add_context_clicked)
        controls.append(add_button)
        controls.append(self.workspace_status)
        spacer_left = Gtk.Box()
        spacer_left.set_hexpand(True)
        controls.append(spacer_left)
        controls.append(self.provider_menu)
        controls.append(self.model_button)
        controls.append(self.auth_button)
        controls.append(self.stop_button)
        controls.append(self.open_artifact_button)
        send_button = Gtk.Button.new_from_icon_name("mail-send-symbolic")
        send_button.add_css_class("send-button")
        send_button.set_tooltip_text("Run prompt")
        send_button.connect("clicked", self._on_send_clicked)
        controls.append(send_button)
        self.send_button = send_button

        composer.append(controls)
        composer_outer.append(composer)
        chat_box.append(composer_outer)

        self.workspace_backdrop = self._build_drawer_backdrop()
        self.workspace_backdrop.add_css_class("workspace-drawer-backdrop")
        self.workspace_backdrop_controller = Gtk.GestureClick.new()
        self.workspace_backdrop_controller.connect("pressed", self._on_workspace_backdrop_pressed)
        self.workspace_backdrop.add_controller(self.workspace_backdrop_controller)
        workspace_overlay.add_overlay(self.workspace_backdrop)

        inspector = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inspector.add_css_class("inspector")
        inspector.set_size_request(INSPECTOR_WIDTH, -1)
        self.inspector_panel = inspector
        self.inspector_revealer = Gtk.Revealer()
        self.inspector_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.inspector_revealer.set_transition_duration(DRAWER_TRANSITION_MS)
        self.inspector_revealer.set_halign(Gtk.Align.END)
        self.inspector_revealer.set_valign(Gtk.Align.FILL)
        self.inspector_revealer.set_vexpand(True)
        self.inspector_revealer.add_css_class("right-drawer")
        self.inspector_revealer.set_reveal_child(False)
        self.inspector_revealer.set_child(inspector)
        workspace_overlay.add_overlay(self.inspector_revealer)

        inspector_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        inspector_header.add_css_class("inspector-header")
        inspector_title = Gtk.Label(label="Workspace")
        inspector_title.add_css_class("inspector-title")
        inspector_title.set_xalign(0)
        inspector_title.set_hexpand(True)
        close_inspector = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_inspector.add_css_class("toolbar-icon")
        close_inspector.set_tooltip_text("Hide workspace panel")
        close_inspector.connect("clicked", self._on_close_inspector_clicked)
        inspector_header.append(inspector_title)
        inspector_header.append(close_inspector)
        inspector.append(inspector_header)

        self.inspector_stack = Gtk.Stack()
        self.inspector_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.inspector_stack.set_transition_duration(220)
        self.inspector_stack.set_vexpand(True)

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.inspector_stack)
        switcher.add_css_class("pane-switcher")
        inspector.append(switcher)

        self.plan_view = self._build_plan_page()
        self.inspector_stack.add_titled(self.plan_view, "plan", "Plan")

        tasks_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        tasks_page.add_css_class("pane-page")
        self.tasks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.tasks_box.set_margin_top(10)
        self.tasks_box.set_margin_bottom(10)
        self.tasks_box.set_margin_start(10)
        self.tasks_box.set_margin_end(10)
        tasks_scroll = Gtk.ScrolledWindow()
        tasks_scroll.set_child(self.tasks_box)
        tasks_scroll.set_vexpand(True)
        tasks_page.append(tasks_scroll)
        self.inspector_stack.add_titled(tasks_page, "tasks", "Tasks")

        self.diff_view = self._build_files_page()
        self.inspector_stack.add_titled(self.diff_view, "diff", "Diff")

        self.artifact_view = self._build_artifact_page()
        self.inspector_stack.add_titled(self.artifact_view, "artifact", "Artifact")

        inspector.append(self.inspector_stack)

        return workspace_overlay

    def _build_surface_page(self, title_text: str, subtitle_text: str) -> tuple[Gtk.Box, Gtk.Box]:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.add_css_class("surface-page")
        outer.set_margin_top(34)
        outer.set_margin_bottom(34)
        outer.set_margin_start(42)
        outer.set_margin_end(42)

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title = Gtk.Label(label=title_text)
        title.add_css_class("surface-title")
        title.set_xalign(0)
        subtitle = Gtk.Label(label=subtitle_text)
        subtitle.add_css_class("surface-subtitle")
        subtitle.set_xalign(0)
        subtitle.set_wrap(True)
        header.append(title)
        header.append(subtitle)
        outer.append(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.add_css_class("surface-content")
        content.set_size_request(360, -1)
        outer.append(content)
        return outer, content

    def _build_search_page(self) -> Gtk.Widget:
        outer, content = self._build_surface_page(
            "Search",
            "Find prior requests, answers, artifacts, and changed files in this workspace.",
        )
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.add_css_class("surface-entry")
        self.search_entry.set_placeholder_text("Search recent chats")
        self.search_entry.connect("search-changed", self._on_search_changed)
        content.append(self.search_entry)

        self.search_results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(self.search_results_box)
        self._refresh_search_results("")
        return outer

    def _build_providers_page(self) -> Gtk.Widget:
        outer, content = self._build_surface_page(
            "Providers",
            "Choose the active agent runtime and inspect the model that will answer the next request.",
        )
        self.provider_cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(self.provider_cards_box)
        self._refresh_provider_cards()
        return outer

    def _build_automations_page(self) -> Gtk.Widget:
        outer, content = self._build_surface_page(
            "Automations",
            "Track planned work, recent runs, and the next action Forge can execute for you.",
        )
        self.automations_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(self.automations_box)
        self._refresh_automations_page()
        return outer

    def _build_settings_page(self) -> Gtk.Widget:
        outer, content = self._build_surface_page(
            "Settings",
            "Workspace, model, and credential controls for the current local project.",
        )
        self.settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(self.settings_box)
        self._refresh_settings_page()
        return outer

    def _make_surface_row(self, title_text: str, meta_text: str = "", icon_name: str | None = None) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("surface-row")
        if icon_name:
            row.append(Gtk.Image.new_from_icon_name(icon_name))
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        copy.set_hexpand(True)
        title = Gtk.Label(label=title_text)
        title.add_css_class("surface-row-title")
        title.set_xalign(0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        copy.append(title)
        if meta_text:
            meta = Gtk.Label(label=meta_text)
            meta.add_css_class("surface-row-meta")
            meta.set_xalign(0)
            meta.set_ellipsize(Pango.EllipsizeMode.END)
            copy.append(meta)
        row.append(copy)
        return row

    def _make_surface_button(self, title_text: str, meta_text: str, icon_name: str | None = None) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("surface-button")
        button.set_child(self._make_surface_row(title_text, meta_text, icon_name))
        return button

    def _build_empty_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.add_css_class("welcome-scroll")
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.add_css_class("welcome-page")
        outer.set_valign(Gtk.Align.START)
        outer.set_halign(Gtk.Align.CENTER)

        title = Gtk.Label(label="What shall we build today?")
        title.add_css_class("welcome-title")
        title.set_justify(Gtk.Justification.CENTER)
        outer.append(title)

        subtitle = Gtk.Label(label="Ask a provider, draft a plan, or run an orchestrated coding task.")
        subtitle.add_css_class("welcome-subtitle")
        subtitle.set_justify(Gtk.Justification.CENTER)
        outer.append(subtitle)

        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chips.set_halign(Gtk.Align.CENTER)
        for mode, text in (("write", "Write"), ("plan", "Plan"), ("code", "Code"), ("review", "Review")):
            chip = Gtk.Button(label=text)
            chip.add_css_class("welcome-chip")
            chip.connect("clicked", self._on_welcome_mode_clicked, mode)
            chips.append(chip)
        outer.append(chips)

        prompts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        prompts.add_css_class("welcome-prompts")
        prompts.set_halign(Gtk.Align.CENTER)
        for title_text, description, prompt, mode in (
            (
                "Plan the next patch",
                "Break down the next changes into a clear executable plan.",
                "Prepare a focused patch plan for the GTK desktop UI and start implementing it.",
                "plan",
            ),
            (
                "Review current changes",
                "Inspect the latest diffs and surface production risks.",
                "Review the current changes and point out bugs, risks, and missing tests.",
                "review",
            ),
            (
                "Polish the interface",
                "Improve UI/UX, spacing, animation, and visual consistency.",
                "Improve the GTK desktop UI polish, spacing, and interaction quality.",
                "code",
            ),
        ):
            button = self._make_welcome_prompt_button(title_text, description, prompt, mode)
            prompts.append(button)
        outer.append(prompts)

        self.welcome_recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.welcome_recent_box.add_css_class("welcome-recents")
        self.welcome_recent_box.set_halign(Gtk.Align.CENTER)
        outer.append(self.welcome_recent_box)
        self._refresh_welcome_recents()

        scroll.set_child(outer)
        return scroll

    def _make_welcome_status(self, title_text: str, value_text: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("welcome-status")
        title = Gtk.Label(label=title_text)
        title.add_css_class("welcome-status-title")
        title.set_xalign(0)
        value = Gtk.Label(label=value_text)
        value.add_css_class("welcome-status-value")
        value.set_xalign(0)
        value.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        value.set_max_width_chars(28)
        box.append(title)
        box.append(value)
        return box

    def _set_welcome_status_value(self, widget: Gtk.Widget, text: str) -> None:
        value = widget.get_last_child() if isinstance(widget, Gtk.Box) else None
        if isinstance(value, Gtk.Label):
            value.set_label(text)

    def _make_welcome_prompt_button(self, title_text: str, description_text: str, prompt: str, mode: str) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("welcome-prompt")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        icon_names = {
            "plan": "view-list-symbolic",
            "review": "edit-find-symbolic",
            "code": "applications-engineering-symbolic",
            "write": "document-edit-symbolic",
        }
        icon_box = self._make_icon_tile(icon_names.get(mode, "system-run-symbolic"), "welcome-prompt-icon")
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        copy.set_hexpand(True)
        title = Gtk.Label(label=title_text)
        title.add_css_class("welcome-prompt-title")
        title.set_xalign(0)
        description = Gtk.Label(label=description_text)
        description.add_css_class("welcome-prompt-meta")
        description.set_xalign(0)
        description.set_wrap(True)
        description.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        arrow.add_css_class("welcome-prompt-arrow")
        copy.append(title)
        copy.append(description)
        row.append(icon_box)
        row.append(copy)
        row.append(arrow)
        button.set_child(row)
        button.connect("clicked", self._on_welcome_prompt_clicked, prompt, mode)
        return button

    def _build_plan_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.add_css_class("pane-page")
        page.add_css_class("plan-page")

        summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        summary.add_css_class("plan-summary")
        self.plan_summary_title = Gtk.Label(label="No plan yet")
        self.plan_summary_title.add_css_class("plan-summary-title")
        self.plan_summary_title.set_xalign(0)
        self.plan_summary_title.set_wrap(True)
        self.plan_summary_meta = Gtk.Label(label="Switch to Plan mode, describe the work, then send.")
        self.plan_summary_meta.add_css_class("plan-summary-meta")
        self.plan_summary_meta.set_xalign(0)
        self.plan_summary_meta.set_wrap(True)
        summary.append(self.plan_summary_title)
        summary.append(self.plan_summary_meta)
        plan_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        plan_actions.add_css_class("plan-actions")
        self.accept_plan_button = Gtk.Button()
        self.accept_plan_button.add_css_class("plan-primary-action")
        self.accept_plan_button.set_child(self._make_button_content("media-playback-start-symbolic", "Accept and run"))
        self.accept_plan_button.set_tooltip_text("Accept this plan and execute the subtasks")
        self.accept_plan_button.set_sensitive(False)
        self.accept_plan_button.connect("clicked", self._on_run_plan_clicked)
        plan_actions.append(self.accept_plan_button)
        summary.append(plan_actions)
        page.append(summary)

        self.plan_cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.plan_cards_box.set_margin_start(10)
        self.plan_cards_box.set_margin_end(10)
        self.plan_cards_box.set_margin_bottom(10)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.plan_cards_box)
        scroll.set_vexpand(True)
        page.append(scroll)
        self._reset_plan_preview()
        return page

    def _render_plan_preview(self, plan) -> None:
        self.plan_summary_title.set_label(plan.strategy or "Generated plan")
        self.plan_summary_meta.set_label(f"{plan.complexity.capitalize()} - {len(plan.subtasks)} steps")
        self._refresh_plan_run_controls()
        self._clear_box(self.plan_cards_box)
        for index, item in enumerate(plan.subtasks, start=1):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            card.add_css_class("plan-card")
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            number = Gtk.Label(label=str(index))
            number.add_css_class("plan-step-number")
            title = Gtk.Label(label=item.title)
            title.add_css_class("plan-step-title")
            title.set_xalign(0)
            title.set_wrap(True)
            title.set_hexpand(True)
            provider = Gtk.Label(label=item.suggested_provider)
            provider.add_css_class("plan-provider")
            provider.add_css_class(self._provider_class(item.suggested_provider))
            top.append(number)
            top.append(title)
            top.append(provider)
            card.append(top)

            deps = ", ".join(item.depends_on) if item.depends_on else "none"
            meta = Gtk.Label(label=f"{item.task_kind} - group {item.parallel_group} - deps {deps}")
            meta.add_css_class("plan-step-meta")
            meta.set_xalign(0)
            meta.set_wrap(True)
            card.append(meta)
            self.plan_cards_box.append(card)
        if plan.ai_rationale:
            rationale = Gtk.Label(label=plan.ai_rationale)
            rationale.add_css_class("plan-rationale")
            rationale.set_xalign(0)
            rationale.set_wrap(True)
            self.plan_cards_box.append(rationale)

    def _reset_plan_preview(self) -> None:
        self.plan_summary_title.set_label("No plan yet")
        self.plan_summary_meta.set_label("Switch to Plan mode, describe the work, then send.")
        self._refresh_plan_run_controls()
        self._clear_box(self.plan_cards_box)
        self.plan_cards_box.append(
            self._make_inspector_empty_state(
                "view-list-symbolic",
                "Plan workspace",
                "Generated plans will appear here with steps, providers, dependencies, and the approval action.",
            )
        )

    def _build_files_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.add_css_class("pane-page")
        page.add_css_class("files-page")

        summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        summary.add_css_class("panel-summary")
        self.files_summary_title = Gtk.Label(label="No file changes")
        self.files_summary_title.add_css_class("panel-summary-title")
        self.files_summary_title.set_xalign(0)
        self.files_summary_meta = Gtk.Label(label="Changed files will appear after a run.")
        self.files_summary_meta.add_css_class("panel-summary-meta")
        self.files_summary_meta.set_xalign(0)
        self.files_summary_meta.set_wrap(True)
        summary.append(self.files_summary_title)
        summary.append(self.files_summary_meta)
        page.append(summary)

        self.files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.files_box.set_margin_start(10)
        self.files_box.set_margin_end(10)
        self.files_box.set_margin_bottom(10)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.files_box)
        scroll.set_vexpand(True)
        page.append(scroll)
        self.files_box.append(self._make_file_empty_state())
        return page

    def _build_artifact_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.add_css_class("pane-page")
        page.add_css_class("artifact-page")

        summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        summary.add_css_class("panel-summary")
        self.artifact_summary_title = Gtk.Label(label="No artifact yet")
        self.artifact_summary_title.add_css_class("panel-summary-title")
        self.artifact_summary_title.set_xalign(0)
        self.artifact_summary_meta = Gtk.Label(label="Run output will appear here.")
        self.artifact_summary_meta.add_css_class("panel-summary-meta")
        self.artifact_summary_meta.set_xalign(0)
        self.artifact_summary_meta.set_wrap(True)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.artifact_file_button = Gtk.Button(label="Open file")
        self.artifact_file_button.add_css_class("panel-action")
        self.artifact_file_button.set_sensitive(False)
        self.artifact_file_button.connect("clicked", self._on_open_artifact_file_clicked)
        files_button = Gtk.Button(label="Files")
        files_button.add_css_class("panel-action")
        files_button.connect("clicked", self._on_show_files_clicked)
        actions.append(self.artifact_file_button)
        actions.append(files_button)
        summary.append(self.artifact_summary_title)
        summary.append(self.artifact_summary_meta)
        summary.append(actions)
        page.append(summary)

        self.artifact_preview = Gtk.Label(label="Waiting for activity.")
        self.artifact_preview.add_css_class("artifact-preview")
        self.artifact_preview.set_xalign(0)
        self.artifact_preview.set_yalign(0)
        self.artifact_preview.set_wrap(True)
        self.artifact_preview.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.artifact_preview.set_selectable(True)
        self.artifact_preview.set_margin_top(10)
        self.artifact_preview.set_margin_bottom(10)
        self.artifact_preview.set_margin_start(10)
        self.artifact_preview.set_margin_end(10)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.artifact_preview)
        scroll.set_vexpand(True)
        page.append(scroll)
        return page

    def _make_inspector_empty_state(self, icon_name: str, title_text: str, meta_text: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("inspector-empty")
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class("inspector-empty-icon")
        title = Gtk.Label(label=title_text)
        title.add_css_class("inspector-empty-title")
        title.set_xalign(0)
        title.set_wrap(True)
        meta = Gtk.Label(label=meta_text)
        meta.add_css_class("inspector-empty-meta")
        meta.set_xalign(0)
        meta.set_wrap(True)
        box.append(icon)
        box.append(title)
        box.append(meta)
        return box

    def _make_text_panel(self, title: str) -> Gtk.Widget:
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        frame.add_css_class("pane-page")
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.add_css_class("panel-text")
        view.get_buffer().set_text("Waiting for activity.")
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(view)
        frame.append(scroll)
        self.panel_views[id(frame)] = view
        return frame

    def _append_system_intro(self) -> None:
        cwd = self.session.file_mgr.get_working_dir()
        self._append_chat_message(
            f"Workspace ready: {cwd}",
            "system",
        )
        self._append_chat_message(
            "Ask Forge to write, plan, code, or review work in this project.",
            "system",
        )

    def _append_chat_message(self, text: str, tag_name: str | None = None) -> None:
        tag_name = tag_name or "assistant"
        _append_text(self.transcript_buffer, text, tag_name)
        provider = normalize_provider_name(self.run_state.active_provider or self.session.current_provider)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        row.add_css_class("message-row")
        row.add_css_class(f"message-row-{tag_name}")
        row.add_css_class(f"provider-{provider}")
        row.set_hexpand(True)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bubble.add_css_class("message-bubble")
        bubble.add_css_class(f"message-bubble-{tag_name}")
        bubble.set_hexpand(False)
        if tag_name == "user":
            bubble.set_halign(Gtk.Align.END)
        elif tag_name in {"system", "event"}:
            bubble.set_halign(Gtk.Align.CENTER)
        else:
            bubble.set_halign(Gtk.Align.START)

        role = {
            "user": "You",
            "assistant": "Forge",
            "error": "Error",
            "event": "",
        }.get(tag_name, "")
        if role:
            role_label = Gtk.Label(label=role)
            role_label.add_css_class("message-role")
            role_label.add_css_class(f"message-role-{tag_name}")
            role_label.set_xalign(0)
            bubble.append(role_label)

        label = Gtk.Label(label=text)
        label.add_css_class("message-text")
        label.add_css_class(f"message-text-{tag_name}")
        label.set_xalign(0)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_selectable(True)
        label.set_max_width_chars(82 if tag_name != "system" else 72)
        bubble.append(label)
        row.append(bubble)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(MESSAGE_TRANSITION_MS)
        revealer.set_child(row)
        revealer.set_reveal_child(False)
        self.message_feed.append(revealer)
        GLib.idle_add(revealer.set_reveal_child, True)
        GLib.idle_add(self._scroll_chat_to_bottom)

    def _clear_chat_messages(self) -> None:
        self.transcript_buffer.set_text("")
        while child := self.message_feed.get_first_child():
            self.message_feed.remove(child)

    def _scroll_chat_to_bottom(self) -> bool:
        adjustment = self.message_scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        return False

    def _panel_set_text(self, frame: Gtk.Widget, text: str) -> None:
        view = self.panel_views[id(frame)]
        view.get_buffer().set_text(text)

    def _show_chat_transcript(self) -> None:
        self._set_active_nav("")
        self._refresh_context_bar()
        self.chat_stack.set_visible_child_name("transcript")

    def _show_inspector(self, pane_name: str, reveal: bool = True) -> None:
        if reveal:
            self._set_inspector_visible(True)
        self.inspector_stack.set_visible_child_name(pane_name)

    def _set_active_nav(self, name: str) -> None:
        for key, button in self.nav_buttons.items():
            if key == name:
                button.add_css_class("sidebar-nav-active")
            else:
                button.remove_css_class("sidebar-nav-active")

    def _fit_drawer_sizes(self) -> None:
        width = self.get_width()
        if width <= 0:
            width, _height = self.get_default_size()
        safe_width = max(260, width - 96) if width > 0 else SIDEBAR_WIDTH
        sidebar_width = min(SIDEBAR_WIDTH, safe_width)
        inspector_width = min(INSPECTOR_WIDTH, max(300, safe_width))
        if hasattr(self, "sidebar_revealer"):
            self.sidebar_revealer.set_size_request(sidebar_width, -1)
        if hasattr(self, "inspector_revealer"):
            self.inspector_revealer.set_size_request(inspector_width, -1)

    def _sync_drawer_backdrops(self) -> None:
        sidebar_visible = self.sidebar_revealer.get_reveal_child() if hasattr(self, "sidebar_revealer") else False
        inspector_visible = self.inspector_revealer.get_reveal_child() if hasattr(self, "inspector_revealer") else False
        if hasattr(self, "app_backdrop"):
            self.app_backdrop.set_visible(sidebar_visible)
            self.app_backdrop.set_can_target(sidebar_visible)
        if hasattr(self, "workspace_backdrop"):
            self.workspace_backdrop.set_visible(inspector_visible)
            self.workspace_backdrop.set_can_target(inspector_visible)

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in {Gdk.KEY_Return, Gdk.KEY_KP_Enter}:
            self._on_send_clicked(self.send_button)
            return True
        if ctrl and keyval in {Gdk.KEY_k, Gdk.KEY_K}:
            self._show_surface("search")
            self.search_entry.grab_focus()
            return True
        if ctrl and keyval in {Gdk.KEY_n, Gdk.KEY_N}:
            self._on_new_session_clicked(Gtk.Button())
            return True
        if keyval == Gdk.KEY_Escape and self.inspector_revealer.get_reveal_child():
            self._set_inspector_visible(False)
            return True
        if keyval == Gdk.KEY_F11:
            self._toggle_fullscreen()
            return True
        return False

    def _toggle_fullscreen(self) -> None:
        if bool(self.get_property("fullscreened")):
            self.unfullscreen()
        else:
            self.fullscreen()
        self._fullscreened = not self._fullscreened

    def _on_window_state_changed(self, _window, _pspec) -> None:
        self._fullscreened = bool(self.get_property("fullscreened"))
        self._fit_drawer_sizes()

    def _on_close_inspector_clicked(self, _button: Gtk.Button) -> None:
        self._set_inspector_visible(False)

    def _set_sidebar_visible(self, visible: bool) -> None:
        if visible:
            self.sidebar_revealer.set_visible(True)
            self._fit_drawer_sizes()
            if hasattr(self, "inspector_revealer") and self.inspector_revealer.get_reveal_child():
                self.inspector_revealer.set_reveal_child(False)
                self.inspector_revealer.set_can_target(False)
                self.inspector_toggle_button.set_tooltip_text("Show workspace panel")
                self.inspector_toggle_button.remove_css_class("toolbar-icon-active")
                self._hide_drawer_after_transition(self.inspector_revealer)
        self.sidebar_revealer.set_reveal_child(visible)
        self.sidebar_revealer.set_can_target(visible)
        self.sidebar_toggle_button.set_tooltip_text("Hide sidebar" if visible else "Show sidebar")
        if visible:
            self.sidebar_toggle_button.add_css_class("toolbar-icon-active")
        else:
            self.sidebar_toggle_button.remove_css_class("toolbar-icon-active")
            self._hide_drawer_after_transition(self.sidebar_revealer)

    def _set_inspector_visible(self, visible: bool) -> None:
        if visible:
            self.inspector_revealer.set_visible(True)
            self._fit_drawer_sizes()
        self.inspector_revealer.set_reveal_child(visible)
        self.inspector_revealer.set_can_target(visible)
        self.inspector_toggle_button.set_tooltip_text("Hide workspace panel" if visible else "Show workspace panel")
        if visible:
            self.inspector_toggle_button.add_css_class("toolbar-icon-active")
        else:
            self.inspector_toggle_button.remove_css_class("toolbar-icon-active")
            self._hide_drawer_after_transition(self.inspector_revealer)
        self._sync_drawer_backdrops()

    def _hide_drawer_after_transition(self, revealer: Gtk.Revealer) -> None:
        def hide_if_still_closed() -> bool:
            if not revealer.get_reveal_child():
                revealer.set_visible(False)
            return False

        GLib.timeout_add(revealer.get_transition_duration() + 20, hide_if_still_closed)

    def _on_app_backdrop_pressed(self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float) -> None:
        self._set_sidebar_visible(False)

    def _on_workspace_backdrop_pressed(self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float) -> None:
        self._set_inspector_visible(False)

    def _on_toggle_sidebar_clicked(self, _button: Gtk.Button) -> None:
        self._set_sidebar_visible(not self.sidebar_revealer.get_reveal_child())

    def _on_toggle_inspector_clicked(self, _button: Gtk.Button) -> None:
        self._set_inspector_visible(not self.inspector_revealer.get_reveal_child())

    def _show_surface(self, name: str) -> None:
        self._set_active_nav(name)
        if name == "providers":
            self._refresh_provider_cards()
        elif name == "automations":
            self._refresh_automations_page()
        elif name == "settings":
            self._refresh_settings_page()
        elif name == "search":
            self._refresh_search_results(self.search_entry.get_text() if hasattr(self, "search_entry") else "")
        self.chat_stack.set_visible_child_name(name)

    def _clear_box(self, box: Gtk.Box) -> None:
        while child := box.get_first_child():
            box.remove(child)

    def _refresh_plan_run_controls(self) -> None:
        sensitive = not self.run_state.busy and self.session.last_plan is not None
        self.run_plan_button.set_sensitive(sensitive)
        if hasattr(self, "accept_plan_button"):
            self.accept_plan_button.set_sensitive(sensitive)

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self.run_state.busy = busy
        self.send_button.set_sensitive(not busy)
        for button in self.mode_buttons.values():
            button.set_sensitive(not busy)
        self._refresh_plan_run_controls()
        self.model_button.set_sensitive(not busy)
        self.auth_button.set_sensitive(not busy)
        self.open_artifact_button.set_sensitive(not busy and self.session.last_task_run is not None)
        self.stop_button.set_sensitive(busy)
        if hasattr(self, "run_spinner"):
            self.run_spinner.set_visible(busy)
            if busy:
                self.run_spinner.start()
            else:
                self.run_spinner.stop()
        if hasattr(self, "composer_box"):
            if busy:
                self.composer_box.add_css_class("composer-busy")
            else:
                self.composer_box.remove_css_class("composer-busy")
        if hasattr(self, "context_bar"):
            if busy:
                self.context_bar.add_css_class("context-running")
            else:
                self.context_bar.remove_css_class("context-running")
        self.status_label.set_label(label or ("Running" if busy else "Ready"))
        self._refresh_context_bar()

    def _provider_class(self, provider: str | None = None) -> str:
        return f"provider-{normalize_provider_name(provider or self.session.current_provider)}"

    def _apply_provider_accent(self) -> None:
        css_class = self._provider_class()
        for widget in self.provider_accent_widgets:
            for class_name in PROVIDER_CLASS_NAMES:
                widget.remove_css_class(class_name)
            widget.add_css_class(css_class)

    def _refresh_context_bar(self) -> None:
        if not hasattr(self, "context_title"):
            return
        cwd = self.session.file_mgr.get_working_dir()
        provider = normalize_provider_name(self.session.current_provider)
        model = self.session.provider_models.get(provider, "").strip() or provider_default_model(provider) or "default"
        mode = self.composer_mode.capitalize()
        state = "Running" if self.run_state.busy else "Ready"
        self.context_title.set_label(cwd.name or str(cwd))
        self.context_meta.set_label(str(cwd))
        if hasattr(self, "context_status"):
            self.context_status.set_label(f"{mode} · {state}")
        if hasattr(self, "context_provider_value"):
            self.context_provider_value.set_label(provider)
        if hasattr(self, "context_model_value"):
            self.context_model_value.set_label(model)
        if hasattr(self, "workspace_status"):
            self.workspace_status.set_label(cwd.name or "Workspace")
            self.workspace_status.set_tooltip_text(f"Working directory: {cwd}")
        if hasattr(self, "sidebar_workspace_meta"):
            self.sidebar_workspace_meta.set_label(str(cwd))
        if hasattr(self, "welcome_project_status"):
            self._set_welcome_status_value(self.welcome_project_status, str(cwd))
        if hasattr(self, "welcome_provider_status"):
            self._set_welcome_status_value(self.welcome_provider_status, provider)
        if hasattr(self, "welcome_model_status"):
            self._set_welcome_status_value(self.welcome_model_status, model)

    def _refresh_provider_state(self) -> None:
        names = list(self.container.provider_paths.keys())
        provider = normalize_provider_name(self.session.current_provider)
        self._apply_provider_accent()
        if provider in names:
            self._suppress_provider_change = True
            self.provider_menu.set_selected(names.index(provider))
            self._suppress_provider_change = False
        for name, button in self.provider_buttons.items():
            button.set_active(name == provider)
            color = PROVIDER_ACCENTS.get(name, "#777777")
            ready, readiness_message = self.container.provider_is_ready(name)
            if name == provider:
                button.add_css_class("active-provider")
            else:
                button.remove_css_class("active-provider")
            if ready:
                button.remove_css_class("provider-needs-setup")
            else:
                button.add_css_class("provider-needs-setup")
            status = "ready" if ready else readiness_message
            button.set_tooltip_text(f"{name} accent {color} - {status}")
        model = self.session.provider_models.get(provider, "").strip() or provider_default_model(provider) or "default"
        self.model_label.set_label(f"{provider} / {model}")
        model_short = model if len(model) <= 24 else model[:21].rstrip() + "..."
        self.model_button.set_label(model_short)
        self.model_button.set_tooltip_text(f"Set model for {provider}: {model}")
        self._refresh_plan_run_controls()
        self.open_artifact_button.set_sensitive(not self.run_state.busy and self.session.last_task_run is not None)
        self._refresh_context_bar()
        if hasattr(self, "provider_cards_box"):
            self._refresh_provider_cards()
        if hasattr(self, "settings_box"):
            self._refresh_settings_page()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._refresh_search_results(entry.get_text())

    def _refresh_search_results(self, query: str) -> None:
        if not hasattr(self, "search_results_box"):
            return
        self._clear_box(self.search_results_box)
        needle = " ".join(query.lower().split())
        runs = self.container.recent_runs(self.session, limit=20)
        if needle:
            runs = [
                run
                for run in runs
                if needle in " ".join(
                    [
                        run.prompt,
                        run.answer_text,
                        run.error_text,
                        run.provider_summary,
                        " ".join(run.touched_files),
                    ]
                ).lower()
            ]
        if not runs:
            self.search_results_box.append(self._make_surface_row("No matching chats", "Try a request, provider, file, or status."))
            return
        for run in runs[:12]:
            title, meta = format_recent_run_label(run, max_prompt_chars=72)
            button = self._make_surface_button(title, meta, "text-x-generic-symbolic")
            button.connect("clicked", self._on_recent_button_clicked, run)
            self.search_results_box.append(button)

    def _refresh_provider_cards(self) -> None:
        if not hasattr(self, "provider_cards_box"):
            return
        self._clear_box(self.provider_cards_box)
        active_provider = normalize_provider_name(self.session.current_provider)
        for provider_name in self.container.provider_paths:
            definition = get_provider_definition(provider_name)
            model = self.session.provider_models.get(provider_name, "").strip() or provider_default_model(provider_name) or "default"
            ready, readiness_message = self.container.provider_is_ready(provider_name)
            state = "Active" if provider_name == active_provider else "Available"
            readiness = "Ready" if ready else readiness_message
            meta = f"{state} - {readiness} - {definition.transport} - {model}"
            button = self._make_surface_button(definition.label, meta, "application-x-addon-symbolic")
            button.add_css_class(f"provider-{provider_name}")
            button.connect("clicked", self._on_provider_card_clicked, provider_name)
            if provider_name == active_provider:
                button.add_css_class("surface-button-active")
            if not ready:
                button.add_css_class("surface-button-warning")
            self.provider_cards_box.append(button)

    def _refresh_automations_page(self) -> None:
        if not hasattr(self, "automations_box"):
            return
        self._clear_box(self.automations_box)
        if self.session.last_plan is not None:
            plan = self.session.last_plan
            run_button = self._make_surface_button(
                "Run current plan",
                f"{len(plan.subtasks)} steps - {plan.complexity} - {plan.strategy}",
                "media-playback-start-symbolic",
            )
            run_button.connect("clicked", self._on_run_plan_clicked)
            self.automations_box.append(run_button)
        else:
            self.automations_box.append(
                self._make_surface_row(
                    "No plan queued",
                    "Switch composer to Plan, describe the work, then send to create an executable plan.",
                    "alarm-symbolic",
                )
            )
        runs = self.container.recent_runs(self.session, limit=6)
        if runs:
            self.automations_box.append(self._make_surface_row("Recent runs", "Open a run to restore its chat and artifacts."))
            for run in runs:
                title, meta = format_recent_run_label(run, max_prompt_chars=72)
                button = self._make_surface_button(title, meta, "view-list-symbolic")
                button.connect("clicked", self._on_recent_button_clicked, run)
                self.automations_box.append(button)

    def _refresh_settings_page(self) -> None:
        if not hasattr(self, "settings_box"):
            return
        self._clear_box(self.settings_box)
        provider = normalize_provider_name(self.session.current_provider)
        model = self.session.provider_models.get(provider, "").strip() or provider_default_model(provider) or "default"
        cwd = str(self.session.file_mgr.get_working_dir())
        key_status = "configured" if (
            self.container.settings.OPENROUTER_API_KEY.strip()
            or self.container.credential_store.has_api_key("openrouter")
        ) else "not configured"
        self.settings_box.append(self._make_surface_row("Working directory", cwd, "folder-symbolic"))
        self.settings_box.append(self._make_surface_row("Active provider", f"{provider} - {model}", "application-x-addon-symbolic"))
        model_button = self._make_surface_button("Model", f"Edit model for {provider}", "preferences-system-symbolic")
        model_button.connect("clicked", self._on_model_clicked)
        self.settings_box.append(model_button)
        key_button = self._make_surface_button("OpenRouter API key", key_status, "dialog-password-symbolic")
        key_button.connect("clicked", self._on_auth_clicked)
        self.settings_box.append(key_button)
        self.settings_box.append(self._make_surface_row("Keyboard shortcuts", "Fast actions for the desktop workspace.", "input-keyboard-symbolic"))
        for title_text, meta_text in (
            ("Send prompt", "Ctrl+Enter"),
            ("Search chats", "Ctrl+K"),
            ("New chat", "Ctrl+N"),
            ("Close workspace panel", "Esc"),
        ):
            self.settings_box.append(self._make_surface_row(title_text, meta_text))

    def _on_provider_card_clicked(self, _button: Gtk.Button, provider_name: str) -> None:
        self._set_provider(provider_name)
        self._refresh_provider_cards()

    def _refresh_session_list(self) -> None:
        while child := self.sessions_list.get_first_child():
            self.sessions_list.remove(child)
        self.recent_row_runs.clear()
        self._refresh_welcome_recents()

        project = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        project.add_css_class("session-row")
        title = Gtk.Label(label=f"Current project")
        title.set_xalign(0)
        title.add_css_class("session-title")
        cwd = Gtk.Label(label=str(self.session.file_mgr.get_working_dir()))
        cwd.set_xalign(0)
        cwd.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        cwd.add_css_class("session-meta")
        project.append(title)
        project.append(cwd)
        project_row = Gtk.ListBoxRow()
        project_row.set_child(project)
        project_row.set_selectable(False)
        project_row.set_activatable(False)
        self.sessions_list.append(project_row)

        recent_runs = self.container.recent_runs(self.session, limit=6)
        if not recent_runs:
            empty = Gtk.Label(label="No chats yet")
            empty.set_xalign(0)
            empty.add_css_class("session-empty")
            empty_row = Gtk.ListBoxRow()
            empty_row.set_child(empty)
            empty_row.set_selectable(False)
            empty_row.set_activatable(False)
            self.sessions_list.append(empty_row)
            return

        for run in recent_runs:
            title_text, meta_text = format_recent_run_label(run)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            content.add_css_class("recent-row")
            title = Gtk.Label(label=title_text)
            title.set_xalign(0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.add_css_class("recent-title")
            meta = Gtk.Label(label=meta_text)
            meta.set_xalign(0)
            meta.set_ellipsize(Pango.EllipsizeMode.END)
            meta.add_css_class("recent-meta")
            content.append(title)
            content.append(meta)
            button = Gtk.Button()
            button.add_css_class("recent-button")
            button.set_child(content)
            button.set_tooltip_text("Open saved chat")
            button.connect("clicked", self._on_recent_button_clicked, run)
            list_row = Gtk.ListBoxRow()
            list_row.set_child(button)
            list_row.set_selectable(True)
            list_row.set_activatable(True)
            self.recent_row_runs[id(list_row)] = run
            self.sessions_list.append(list_row)

    def _refresh_welcome_recents(self) -> None:
        if self.welcome_recent_box is None:
            return
        self._clear_box(self.welcome_recent_box)
        runs = self.container.recent_runs(self.session, limit=3)
        if not runs:
            return
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.add_css_class("welcome-recents-header")
        heading = Gtk.Label(label="Recent work")
        heading.add_css_class("welcome-recents-title")
        heading.set_xalign(0)
        heading.set_hexpand(True)
        view_all = Gtk.Button(label="View all")
        view_all.add_css_class("welcome-recents-action")
        view_all.connect("clicked", self._on_automations_nav_clicked)
        header.append(heading)
        header.append(view_all)
        self.welcome_recent_box.append(header)
        for run in runs:
            title_text, meta_text = format_recent_run_label(run, max_prompt_chars=56)
            button = Gtk.Button()
            button.add_css_class("welcome-recent")
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            status = Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if run.status == "success" else "dialog-warning-symbolic")
            status.add_css_class("welcome-recent-status")
            copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            copy.set_hexpand(True)
            title = Gtk.Label(label=title_text)
            title.add_css_class("welcome-recent-title")
            title.set_xalign(0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            meta = Gtk.Label(label=meta_text)
            meta.add_css_class("welcome-recent-meta")
            meta.set_xalign(0)
            meta.set_ellipsize(Pango.EllipsizeMode.END)
            arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
            arrow.add_css_class("welcome-recent-arrow")
            copy.append(title)
            copy.append(meta)
            content.append(status)
            content.append(copy)
            content.append(arrow)
            button.set_child(content)
            button.connect("clicked", self._on_recent_button_clicked, run)
            self.welcome_recent_box.append(button)

    def _on_recent_button_clicked(self, _button: Gtk.Button, task_run) -> None:
        self._open_recent_run(task_run, force=True)

    def _on_recent_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        task_run = self.recent_row_runs.get(id(row))
        if task_run is None:
            return
        self._open_recent_run(task_run, force=True)

    def _on_recent_row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
        task_run = self.recent_row_runs.get(id(row))
        if task_run is None:
            return
        self._open_recent_run(task_run)

    def _open_recent_run(self, task_run, force: bool = False) -> None:
        if not force and self.active_recent_run_id == task_run.run_id:
            return
        self.active_recent_run_id = task_run.run_id
        self.session.last_task_run = task_run
        self._clear_chat_messages()
        for tag_name, text in render_run_chat_messages(task_run):
            self._append_chat_message(text, tag_name)
        self._show_chat_transcript()
        self._update_file_panel_from_run(task_run)
        self._render_task_run_rows(task_run)
        self._update_artifact_panel_from_run(task_run)
        self.open_artifact_button.set_sensitive(True)
        self.status_label.set_label("Recent opened")

    def _refresh_run_history(self) -> None:
        while child := self.tasks_box.get_first_child():
            self.tasks_box.remove(child)
        self.active_task_rows.clear()
        runs = self.container.recent_runs(self.session, limit=5)
        if not runs:
            self.tasks_box.append(self._make_task_empty_state("No completed runs yet."))
            return
        for run in runs:
            title, meta = format_recent_run_label(run, max_prompt_chars=54)
            event = {
                "subtask_id": run.run_id,
                "title": title,
                "provider": run.provider_summary or "mixed",
                "status": run.status,
                "text": meta,
            }
            self.tasks_box.append(self._make_task_card(event)["card"])

    def _render_task_run_rows(self, task_run) -> None:
        self._clear_tasks()
        if not task_run.subtasks:
            event = {
                "subtask_id": task_run.run_id,
                "title": task_run.prompt or "Single-agent execution",
                "provider": task_run.provider_summary or "mixed",
                "status": task_run.status,
                "text": task_run.mode,
            }
            self.tasks_box.append(self._make_task_card(event)["card"])
            return
        for subtask in task_run.subtasks:
            event = {
                "subtask_id": subtask.subtask_id,
                "title": subtask.title,
                "provider": subtask.provider,
                "status": subtask.status,
                "text": subtask.error_text or subtask.handoff_summary or subtask.description,
                "parallel_group": getattr(subtask, "parallel_group", None),
            }
            self.tasks_box.append(self._make_task_card(event)["card"])

    def _clear_tasks(self, placeholder: str = "") -> None:
        while child := self.tasks_box.get_first_child():
            self.tasks_box.remove(child)
        self.active_task_rows.clear()
        if placeholder:
            self.tasks_box.append(self._make_task_empty_state(placeholder))

    def _seed_plan_tasks(self, plan) -> None:
        self._clear_tasks()
        for item in plan.subtasks:
            payload = {
                "subtask_id": item.subtask_id,
                "provider": item.suggested_provider,
                "title": item.title,
                "status": "pending",
                "text": "Ready",
                "parallel_group": item.parallel_group,
            }
            self._update_task_event(payload)

    def _handle_stream_event(self, line: str) -> None:
        event = decode_forge_event(line)
        if event and event.get("type") == "agent_status":
            self._update_task_event(event)
            return
        text = format_stream_event_for_chat(line)
        if text:
            self._append_chat_message(text, "event")

    def _update_task_event(self, event: dict) -> None:
        subtask_id = str(event.get("subtask_id") or "").strip()
        if not subtask_id:
            return
        status = str(event.get("status") or "running").strip()
        provider = str(event.get("provider") or "mixed").strip()
        row = self.active_task_rows.get(subtask_id)
        if row is None:
            row = self._make_task_card(event)
            self.tasks_box.append(row["card"])
            self.active_task_rows[subtask_id] = row
        self._update_task_card(row, event)
        self.status_label.set_label(f"{provider}: {status}")
        self._show_inspector("tasks", reveal=False)

    def _update_file_panel_from_result(self, result) -> None:
        self._render_files_panel(
            files=result.touched_files,
            title="Files changed",
            meta=f"{result.provider} - exit {result.exit_code} - {result.duration_text}",
        )
        self._show_inspector("diff", reveal=False)

    def _update_file_panel_from_run(self, task_run) -> None:
        self._render_files_panel(
            files=task_run.touched_files,
            title=f"{len(task_run.touched_files)} touched files" if task_run.touched_files else "No file changes",
            meta=f"{task_run.status} - {task_run.mode} - {task_run.provider_summary or 'mixed'}",
            artifact_file=task_run.artifact_file,
        )
        self._show_inspector("diff", reveal=False)

    def _update_artifact_panel_from_run(self, task_run) -> None:
        self._render_artifact_panel(task_run)
        self._show_inspector("artifact", reveal=False)

    def _make_task_empty_state(self, text: str) -> Gtk.Widget:
        return self._make_inspector_empty_state("alarm-symbolic", "No task activity", text)

    def _task_state_label(self, status: str) -> str:
        normalized = (status or "pending").strip().lower()
        return {
            "pending": "Pending",
            "running": "Running",
            "success": "Done",
            "failed": "Failed",
            "partial": "Partial",
            "skipped": "Skipped",
            "reused": "Reused",
        }.get(normalized, normalized.capitalize() or "Pending")

    def _make_task_card(self, event: dict) -> dict[str, Gtk.Widget]:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        card.add_css_class("task-card")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label()
        title.add_css_class("task-title")
        title.set_xalign(0)
        title.set_wrap(True)
        title.set_hexpand(True)
        status = Gtk.Label()
        status.add_css_class("task-status")
        top.append(title)
        top.append(status)
        card.append(top)

        meta = Gtk.Label()
        meta.add_css_class("task-meta")
        meta.set_xalign(0)
        meta.set_wrap(True)
        card.append(meta)

        detail = Gtk.Label()
        detail.add_css_class("task-detail")
        detail.set_xalign(0)
        detail.set_wrap(True)
        detail.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        detail.set_visible(False)
        card.append(detail)

        row = {"card": card, "title": title, "status": status, "meta": meta, "detail": detail}
        self._update_task_card(row, event)
        return row

    def _update_task_card(self, row: dict[str, Gtk.Widget], event: dict) -> None:
        status_value = str(event.get("status") or "running").strip()
        provider = str(event.get("provider") or "mixed").strip()
        title = str(event.get("title") or event.get("subtask_id") or "Task").strip()
        detail = str(event.get("text") or "").strip()
        group = event.get("parallel_group")
        group_text = f"Group {group}" if group not in {None, "", 0, "0"} else "Single"

        title_label = row["title"]
        status_label = row["status"]
        meta_label = row["meta"]
        detail_label = row["detail"]
        if isinstance(title_label, Gtk.Label):
            title_label.set_label(title)
        if isinstance(status_label, Gtk.Label):
            status_label.set_label(self._task_state_label(status_value))
        if isinstance(meta_label, Gtk.Label):
            meta_label.set_label(f"{provider} - {group_text}")
        if isinstance(detail_label, Gtk.Label):
            detail_label.set_label(detail)
            detail_label.set_visible(bool(detail))

        card = row["card"]
        for css_class in ("task-running", "task-success", "task-failed"):
            card.remove_css_class(css_class)
            status_label.remove_css_class(css_class)
        if status_value == "running":
            card.add_css_class("task-running")
            status_label.add_css_class("task-running")
        elif status_value in {"success", "reused"}:
            card.add_css_class("task-success")
            status_label.add_css_class("task-success")
        elif status_value in {"failed", "partial", "skipped"}:
            card.add_css_class("task-failed")
            status_label.add_css_class("task-failed")

    def _render_files_panel(self, files: list[str], title: str, meta: str, artifact_file: str = "") -> None:
        unique_files = list(dict.fromkeys(files))
        self.files_summary_title.set_label(title)
        if unique_files:
            suffix = f" - artifact saved" if artifact_file else ""
            self.files_summary_meta.set_label(f"{meta}{suffix}")
        else:
            self.files_summary_meta.set_label(meta or "No file changes reported by this run.")
        self._clear_box(self.files_box)

        if not unique_files:
            self.files_box.append(self._make_file_empty_state())
            return

        for file_path in unique_files:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            card.add_css_class("file-card")

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name = Gtk.Label(label=file_path)
            name.add_css_class("file-title")
            name.set_xalign(0)
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            name.set_hexpand(True)
            open_button = Gtk.Button.new_from_icon_name("document-open-symbolic")
            open_button.add_css_class("file-open")
            open_button.set_tooltip_text("Open file")
            open_button.connect("clicked", self._on_open_file_clicked, file_path)
            top.append(name)
            top.append(open_button)
            card.append(top)

            summary = self._git_diff_summary(file_path) or "No unstaged diff against HEAD"
            diff = Gtk.Label(label=summary)
            diff.add_css_class("file-meta")
            diff.set_xalign(0)
            card.append(diff)
            preview_lines = self._git_diff_preview(file_path)
            if preview_lines:
                preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                preview_box.add_css_class("diff-preview")
                for css_class, line_text in preview_lines:
                    line = Gtk.Label(label=line_text)
                    line.add_css_class("diff-line")
                    line.add_css_class(css_class)
                    line.set_xalign(0)
                    line.set_ellipsize(Pango.EllipsizeMode.END)
                    line.set_selectable(True)
                    preview_box.append(line)
                card.append(preview_box)
            self.files_box.append(card)

    def _make_file_empty_state(self) -> Gtk.Widget:
        return self._make_inspector_empty_state(
            "text-x-generic-symbolic",
            "No changed files",
            "Diff previews and touched files will appear here after a provider edits the workspace.",
        )

    def _render_artifact_panel(self, task_run) -> None:
        preview = render_run_artifact_summary(task_run)
        if len(preview) > 3600:
            preview = preview[:3600].rstrip() + "\n\n... truncated ..."
        title = "Artifact ready" if task_run.artifact_file else "Run details"
        meta_parts = [task_run.status or "pending", task_run.mode or "single"]
        if task_run.provider_summary:
            meta_parts.append(task_run.provider_summary)
        if task_run.duration_ms:
            meta_parts.append(task_run.duration_text)
        self.artifact_summary_title.set_label(title)
        self.artifact_summary_meta.set_label(" - ".join(meta_parts))
        self.artifact_preview.set_label(preview or "No artifact content available.")
        self.artifact_file_button.set_sensitive(bool(task_run.artifact_file))

    def _resolve_workspace_path(self, path: str) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        return self.session.file_mgr.get_working_dir() / candidate

    def _open_path_in_default_app(self, path: str) -> None:
        target = self._resolve_workspace_path(path)
        if not target.exists():
            self._append_chat_message(f"File not found: {target}", "error")
            return
        try:
            Gio.AppInfo.launch_default_for_uri(target.as_uri(), None)
            self.status_label.set_label("Opened file")
        except Exception as exc:
            self._append_chat_message(f"Open failed: {exc}", "error")

    def _on_open_file_clicked(self, _button: Gtk.Button, path: str) -> None:
        self._open_path_in_default_app(path)

    def _on_open_artifact_file_clicked(self, _button: Gtk.Button) -> None:
        if self.session.last_task_run is None or not self.session.last_task_run.artifact_file:
            return
        self._open_path_in_default_app(self.session.last_task_run.artifact_file)

    def _on_show_files_clicked(self, _button: Gtk.Button) -> None:
        if self.session.last_task_run is not None:
            self._update_file_panel_from_run(self.session.last_task_run)
        self._show_inspector("diff")

    def _git_diff_summary(self, path: str) -> str:
        try:
            cwd = self.session.file_mgr.get_working_dir()
            rel = self._relative_workspace_path(path)
            proc = subprocess.run(
                ["git", "diff", "--numstat", "HEAD", "--", str(rel)],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=4,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                added, removed, *_ = proc.stdout.strip().split(maxsplit=2)
                return f"+{added} -{removed}"
        except Exception as exc:
            log.debug("Failed to get git diff summary for %s: %s", path, exc)
        return ""

    def _relative_workspace_path(self, path: str) -> Path:
        cwd = self.session.file_mgr.get_working_dir()
        root = Path(cwd).resolve()
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            return candidate
        try:
            return candidate.resolve().relative_to(root)
        except Exception:
            return Path(candidate.name)

    def _git_diff_preview(self, path: str, max_lines: int = 80) -> list[tuple[str, str]]:
        try:
            cwd = self.session.file_mgr.get_working_dir()
            rel = self._relative_workspace_path(path)
            proc = subprocess.run(
                ["git", "diff", "--no-ext-diff", "--unified=3", "HEAD", "--", str(rel)],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=4,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return parse_unified_diff_preview(proc.stdout, max_lines=max_lines)
            target = self._resolve_workspace_path(path)
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(rel)],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=4,
            )
            if tracked.returncode != 0 and target.is_file():
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                preview = [("diff-hunk", f"@@ new file: {rel} @@")]
                preview.extend(("diff-added", f"+{line}") for line in lines[: max_lines - 1])
                if len(lines) >= max_lines:
                    preview.append(("diff-context", "..."))
                return preview
        except Exception as exc:
            log.debug("Failed to build git diff preview for %s: %s", path, exc)
        return []

    def _get_prompt_text(self) -> str:
        buffer = self.prompt_entry.get_buffer()
        start = buffer.get_start_iter()
        end = buffer.get_end_iter()
        return buffer.get_text(start, end, False).strip()

    def _clear_prompt(self) -> None:
        self.prompt_entry.get_buffer().set_text("")

    def _set_prompt_text(self, text: str) -> None:
        self.prompt_entry.get_buffer().set_text(text)
        self.prompt_entry.grab_focus()

    def _insert_prompt_text(self, text: str) -> None:
        buffer = self.prompt_entry.get_buffer()
        buffer.insert_at_cursor(text)
        self.prompt_entry.grab_focus()

    def _context_candidates(self) -> list[tuple[str, str, str]]:
        cwd = Path(self.session.file_mgr.get_working_dir())
        candidates = [
            ("README", "README.md", "Project overview and usage notes"),
            ("Package", "pyproject.toml", "Python package, scripts, and test config"),
            ("GTK app", "desktop/gtk/app.py", "Main desktop UI controller"),
            ("GTK styles", "desktop/gtk/styles/forge.css", "Desktop visual system"),
            ("Tests", "tests", "Unit tests and behavior checks"),
            ("Docs", "docs", "Project planning and documentation"),
        ]
        existing = [(title, token, meta) for title, token, meta in candidates if (cwd / token).exists()]
        return existing or [("Current project", ".", "Use the whole working directory as context")]

    def _set_composer_mode(self, mode: str) -> None:
        self.composer_mode = mode
        hint_by_mode = {
            "write": "Write with project context",
            "plan": "Plan before running",
            "code": "Implement in this workspace",
            "review": "Review changes and risks",
        }
        tooltip_by_mode = {
            "write": "Send prompt",
            "plan": "Create plan",
            "code": "Run coding prompt",
            "review": "Run review prompt",
        }
        for key, button in self.mode_buttons.items():
            if key == mode:
                button.add_css_class("mode-chip-active")
            else:
                button.remove_css_class("mode-chip-active")
        self.prompt_hint.set_label(hint_by_mode.get(mode, hint_by_mode["write"]))
        self.send_button.set_tooltip_text(tooltip_by_mode.get(mode, "Send prompt"))
        self.status_label.set_label(mode.capitalize())
        self._refresh_context_bar()
        self.prompt_entry.grab_focus()

    def _on_welcome_mode_clicked(self, _button: Gtk.Button, mode: str) -> None:
        self._set_composer_mode(mode)
        if mode == "code" and not self._get_prompt_text():
            self._set_prompt_text("Implement ")
        elif mode == "review" and not self._get_prompt_text():
            self._set_prompt_text("Review the current changes and point out bugs, risks, and missing tests.")

    def _on_welcome_prompt_clicked(self, _button: Gtk.Button, prompt: str, mode: str) -> None:
        self._set_composer_mode(mode)
        self._set_prompt_text(prompt)

    def _on_add_context_clicked(self, button: Gtk.Button) -> None:
        popover = Gtk.Popover()
        popover.add_css_class("context-popover")
        popover.set_parent(button)
        if hasattr(popover, "set_has_arrow"):
            popover.set_has_arrow(False)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)

        title = Gtk.Label(label="Add context")
        title.add_css_class("context-popover-title")
        title.set_xalign(0)
        content.append(title)

        for label_text, token, meta_text in self._context_candidates():
            item = Gtk.Button()
            item.add_css_class("context-option")
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            label = Gtk.Label(label=label_text)
            label.add_css_class("context-option-title")
            label.set_xalign(0)
            meta = Gtk.Label(label=f"@{token} - {meta_text}")
            meta.add_css_class("context-option-meta")
            meta.set_xalign(0)
            meta.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            row.append(label)
            row.append(meta)
            item.set_child(row)
            item.connect("clicked", self._on_context_option_clicked, popover, token)
            content.append(item)

        popover.set_child(content)
        popover.popup()
        self.status_label.set_label("Add context")

    def _on_context_option_clicked(self, _button: Gtk.Button, popover: Gtk.Popover, token: str) -> None:
        prompt = self._get_prompt_text()
        prefix = "" if not prompt or prompt.endswith((" ", "\n")) else " "
        self._insert_prompt_text(f"{prefix}@{token} ")
        popover.popdown()

    def _on_provider_dropdown_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._suppress_provider_change:
            return
        names = list(self.container.provider_paths.keys())
        selected = dropdown.get_selected()
        if selected < len(names):
            self._set_provider(names[selected])

    def _on_provider_button_clicked(self, _button: Gtk.ToggleButton, provider_name: str) -> None:
        self._set_provider(provider_name)

    def _set_provider(self, provider_name: str) -> None:
        provider = normalize_provider_name(provider_name)
        if self.session.current_provider == provider:
            self._refresh_provider_state()
            return
        self.session.current_provider = provider
        self.container.save_session(self.session)
        self._refresh_provider_state()
        self._append_chat_message(f"Provider set to {provider}.", "system")

    def _on_new_session_clicked(self, _button: Gtk.Button) -> None:
        next_id = max([self.chat_id, *self.container.sessions.keys()], default=0) + 1
        self.chat_id = next_id
        self.session = self.container.get_session(next_id)
        self.active_recent_run_id = ""
        self._clear_chat_messages()
        self._set_active_nav("")
        self.chat_stack.set_visible_child_name("welcome")
        self._set_inspector_visible(False)
        self._reset_plan_preview()
        self._refresh_session_list()
        self._refresh_provider_state()
        self._refresh_run_history()
        self._append_system_intro()

    def _on_search_nav_clicked(self, _button: Gtk.Button) -> None:
        self._show_surface("search")
        self.search_entry.grab_focus()

    def _on_providers_nav_clicked(self, _button: Gtk.Button) -> None:
        self._show_surface("providers")

    def _on_automations_nav_clicked(self, _button: Gtk.Button) -> None:
        self._show_surface("automations")

    def _on_settings_nav_clicked(self, _button: Gtk.Button) -> None:
        self._show_surface("settings")

    def _on_change_workspace_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileChooserNative(
            title="Choose working directory",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Use folder",
            cancel_label="Cancel",
        )
        cwd = self.session.file_mgr.get_working_dir()
        try:
            dialog.set_current_folder(Gio.File.new_for_path(str(cwd)))
        except Exception as exc:
            log.debug("Failed to preselect workspace folder %s: %s", cwd, exc)
        dialog.connect("response", self._on_workspace_dialog_response)
        dialog.show()

    def _on_workspace_dialog_response(self, dialog: Gtk.FileChooserNative, response: int) -> None:
        try:
            if response != Gtk.ResponseType.ACCEPT:
                return
            selected = dialog.get_file()
            path = Path(selected.get_path()) if selected is not None and selected.get_path() else None
            if path is None:
                self.status_label.set_label("No directory selected")
                return
            self._set_working_directory(path)
        finally:
            dialog.destroy()

    def _set_working_directory(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        if not resolved.exists() or not resolved.is_dir():
            self.status_label.set_label("Directory unavailable")
            self._append_chat_message(f"Directory unavailable: {resolved}", "error")
            return
        self.session.file_mgr.set_working_dir(str(resolved))
        self.container.base_working_dir = self.session.file_mgr.get_working_dir()
        self.container.save_session(self.session)
        self.active_recent_run_id = ""
        self.status_label.set_label("Workspace changed")
        self._refresh_context_bar()
        self._refresh_session_list()
        self._refresh_run_history()
        if self.chat_stack.get_visible_child_name() == "settings":
            self._refresh_settings_page()
        self._append_chat_message(f"Working directory set to {self.session.file_mgr.get_working_dir()}", "system")

    def _on_send_clicked(self, _button: Gtk.Button) -> None:
        prompt = self._get_prompt_text()
        if not prompt and self.composer_mode == "review":
            prompt = "Review the current changes and point out bugs, risks, and missing tests."
            self._set_prompt_text(prompt)
        if not prompt:
            self.status_label.set_label("Write a request first")
            self.prompt_entry.grab_focus()
            return
        self._set_inspector_visible(False)
        self._clear_prompt()
        self._show_chat_transcript()
        if self.composer_mode == "plan":
            self._start_plan_run(prompt)
        else:
            self._start_prompt_run(prompt)

    def _on_prompt_focus_entered(self, _controller: Gtk.EventControllerFocus) -> None:
        self._set_inspector_visible(False)

    def _on_write_mode_clicked(self, _button: Gtk.Button) -> None:
        self._set_composer_mode("write")

    def _on_plan_clicked(self, _button: Gtk.Button) -> None:
        self._set_composer_mode("plan")

    def _on_run_plan_clicked(self, _button: Gtk.Button) -> None:
        if self.session.last_plan is None:
            self.status_label.set_label("Create a plan first")
            self.prompt_entry.grab_focus()
            return
        self._show_chat_transcript()
        self._start_orchestrated_run()

    def _on_code_mode_clicked(self, _button: Gtk.Button) -> None:
        self._set_composer_mode("code")
        if not self._get_prompt_text():
            self._set_prompt_text("Implement ")

    def _on_review_mode_clicked(self, _button: Gtk.Button) -> None:
        self._set_composer_mode("review")
        if not self._get_prompt_text():
            self._set_prompt_text("Review the current changes and point out bugs, risks, and missing tests.")

    def _on_open_artifact_clicked(self, _button: Gtk.Button) -> None:
        if self.session.last_task_run is None:
            self._append_chat_message("No run artifact yet.", "system")
            return
        self._update_artifact_panel_from_run(self.session.last_task_run)
        self._show_inspector("artifact")
        self._append_chat_message("Loaded latest run artifact into the Artifact pane.", "system")

    def _on_model_clicked(self, _button: Gtk.Button) -> None:
        provider = normalize_provider_name(self.session.current_provider)
        dialog = Gtk.Dialog(title=f"Model - {provider}", transient_for=self, modal=True)
        dialog.add_css_class("forge-dialog")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Use default", Gtk.ResponseType.NO)
        if provider == "openrouter":
            dialog.add_button("Refresh catalog", Gtk.ResponseType.APPLY)
        if provider == "local":
            dialog.add_button("Download selected", Gtk.ResponseType.APPLY)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.add_css_class("forge-dialog-content")
        content.set_spacing(10)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        hint = Gtk.Label(label=f"Current default: {provider_default_model(provider) or 'provider default'}")
        hint.set_xalign(0)
        hint.add_css_class("muted")
        content.append(hint)

        entry = Gtk.Entry()
        entry.set_placeholder_text("model name")
        entry.set_text(self.session.provider_models.get(provider, "").strip())
        content.append(entry)

        model_rows: dict[int, str] = {}
        search = Gtk.SearchEntry()
        search.add_css_class("surface-entry")
        search.set_placeholder_text(
            "Search models by name, alias, or capability"
            if provider != "openrouter"
            else "Search OpenRouter models: sonnet, qwen, deepseek, free"
        )
        if provider == "local":
            search.set_placeholder_text("Search local models: qwen, devstral, llama, mistral")
        content.append(search)

        results = Gtk.ListBox()
        results.add_css_class("model-results")
        results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        results.set_activate_on_single_click(True)
        content.append(results)
        search.connect("search-changed", self._on_model_search_changed, provider, results, model_rows)
        results.connect("row-activated", self._on_model_result_activated, entry, model_rows)
        self._populate_model_results(provider, results, model_rows, "")

        thinking_controls: dict[str, Gtk.ToggleButton] | None = None
        current_model = self.session.provider_models.get(provider, "").strip() or provider_default_model(provider)
        if provider_supports_thinking(provider, current_model):
            thinking_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            thinking_box.add_css_class("model-thinking-row")
            thinking_label = Gtk.Label(label="Thinking")
            thinking_label.set_xalign(0)
            thinking_label.set_hexpand(True)
            thinking_label.add_css_class("muted")
            thinking_segment = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            thinking_segment.add_css_class("model-thinking-segment")
            thinking_controls = {}
            mode = get_thinking_mode(self.session)
            first_button = None
            for item in ("off", "compact", "full"):
                button = Gtk.ToggleButton(label=item)
                button.add_css_class("model-thinking-toggle")
                if first_button is None:
                    first_button = button
                else:
                    button.set_group(first_button)
                button.set_active(item == mode)
                thinking_controls[item] = button
                thinking_segment.append(button)
            if not any(button.get_active() for button in thinking_controls.values()):
                thinking_controls["compact"].set_active(True)
            thinking_box.append(thinking_label)
            thinking_box.append(thinking_segment)
            content.append(thinking_box)
        else:
            thinking_note = Gtk.Label(label="Thinking controls are not exposed by this provider/model.")
            thinking_note.set_xalign(0)
            thinking_note.add_css_class("model-empty")
            content.append(thinking_note)

        dialog.connect("response", self._on_model_dialog_response, provider, entry, search, results, model_rows, thinking_controls)
        dialog.present()

    def _populate_model_results(self, provider: str, results: Gtk.ListBox, model_rows: dict[int, str], query: str, refresh: bool = False) -> None:
        while child := results.get_first_child():
            results.remove(child)
        model_rows.clear()
        if refresh and provider == "openrouter":
            models = self.container.list_available_models("openrouter", refresh=True)
        elif refresh and provider == "local":
            models = self.container.list_available_models("local", refresh=True)
        else:
            query = query.strip()
            models = (
                self.container.openrouter_catalog.search_models(query, limit=12)
                if provider == "openrouter" and query
                else self.container.local_model_catalog.search_models(query, limit=12)
                if provider == "local" and query
                else [
                    item
                    for item in self.container.list_available_models(provider)
                    if not query
                    or query.casefold() in " ".join((item.name, item.label, item.description, *item.aliases)).casefold()
                ][:12]
            )
        if not models:
            empty = Gtk.Label(label="No matching models")
            empty.add_css_class("model-empty")
            empty.set_xalign(0)
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            row.set_child(empty)
            results.append(row)
            return
        for item in models[:12]:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("model-result")
            title = Gtk.Label(label=item.label or item.name)
            title.add_css_class("model-result-title")
            title.set_xalign(0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            name = Gtk.Label(label=item.name)
            name.add_css_class("model-result-id")
            name.set_xalign(0)
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            box.append(title)
            box.append(name)
            if item.description:
                desc = Gtk.Label(label=item.description)
                desc.add_css_class("model-result-desc")
                desc.set_xalign(0)
                desc.set_wrap(True)
                desc.set_lines(2)
                box.append(desc)
            row = Gtk.ListBoxRow()
            row.set_child(box)
            model_rows[id(row)] = item.name
            results.append(row)

    def _on_model_search_changed(self, search: Gtk.SearchEntry, provider: str, results: Gtk.ListBox, model_rows: dict[int, str]) -> None:
        self._populate_model_results(provider, results, model_rows, search.get_text())

    def _on_model_result_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow, entry: Gtk.Entry, model_rows: dict[int, str]) -> None:
        model = model_rows.get(id(row), "")
        if model:
            entry.set_text(model)

    def _on_model_dialog_response(
        self,
        dialog: Gtk.Dialog,
        response: int,
        provider: str,
        entry: Gtk.Entry,
        search: Gtk.SearchEntry | None = None,
        results: Gtk.ListBox | None = None,
        model_rows: dict[int, str] | None = None,
        thinking_controls: dict[str, Gtk.ToggleButton] | None = None,
    ) -> None:
        if response == Gtk.ResponseType.APPLY and provider == "openrouter" and results is not None:
            self._populate_model_results(provider, results, model_rows or {}, search.get_text() if search else "", refresh=True)
            self.status_label.set_label("OpenRouter catalog refreshed")
            return
        if response == Gtk.ResponseType.APPLY and provider == "local":
            model = entry.get_text().strip()
            if not model:
                selected = results.get_selected_row() if results is not None else None
                model = (model_rows or {}).get(id(selected), "") if selected is not None else ""
            if not model:
                self._append_chat_message("Choose a local model to download first.", "error")
                return
            self._download_local_model_from_dialog(model, dialog, search, results, model_rows or {})
            return
        if response == Gtk.ResponseType.OK:
            model = entry.get_text().strip()
            if model:
                resolution = self.container.resolve_model_selection(provider, model)
                if resolution.status == "ambiguous" and results is not None:
                    self._populate_model_results(provider, results, model_rows or {}, model)
                    self._append_chat_message(resolution.message or f"Pick a more specific {provider} model.", "system")
                    return
                if resolution.status in {"ambiguous", "missing", "empty"}:
                    self._append_chat_message(resolution.message, "error")
                    dialog.destroy()
                    return
                resolved_model = resolution.model_name or model
                if provider == "local" and resolved_model and not self.container.local_model_is_installed(resolved_model, refresh=True):
                    self._download_local_model_from_dialog(resolved_model, dialog, search, results, model_rows or {})
                    return
                self.session.provider_models[provider] = resolved_model
            else:
                self.session.provider_models[provider] = ""
            if thinking_controls is not None:
                selected_mode = next((mode for mode, button in thinking_controls.items() if button.get_active()), "compact")
                set_thinking_mode(self.session, selected_mode)
            self.container.reset_runtime(self.session, provider)
            self.container.save_session(self.session)
            self._refresh_provider_state()
            self._append_chat_message(f"Model set for {provider}.", "system")
        elif response == Gtk.ResponseType.NO:
            self.session.provider_models[provider] = ""
            self.container.reset_runtime(self.session, provider)
            self.container.save_session(self.session)
            self._refresh_provider_state()
            self._append_chat_message(f"Model reset for {provider}.", "system")
        dialog.destroy()

    def _download_local_model_from_dialog(
        self,
        model: str,
        dialog: Gtk.Dialog,
        search: Gtk.SearchEntry | None,
        results: Gtk.ListBox | None,
        model_rows: dict[int, str],
    ) -> None:
        self.status_label.set_label(f"Downloading local model {model}...")

        def worker() -> None:
            result = self._run_coro_in_thread(self.container.pull_local_model(model))

            def finish() -> bool:
                if not result.ok:
                    self._append_chat_message(result.message, "error")
                    self.status_label.set_label("Local model download failed")
                    return False
                self.session.provider_models["local"] = result.model_name
                self.container.reset_runtime(self.session, "local")
                self.container.save_session(self.session)
                if results is not None:
                    self._populate_model_results("local", results, model_rows, search.get_text() if search else "", refresh=True)
                self._refresh_provider_state()
                self._append_chat_message(f"Downloaded and selected local model {result.model_name}.", "system")
                self.status_label.set_label("Local model ready")
                dialog.destroy()
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()

    def _on_auth_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.Dialog(title="OpenRouter API key", transient_for=self, modal=True)
        dialog.add_css_class("forge-dialog")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Remove saved", Gtk.ResponseType.NO)
        dialog.add_button("Save", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.add_css_class("forge-dialog-content")
        content.set_spacing(10)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        source = "environment" if self.container.settings.OPENROUTER_API_KEY.strip() else (
            "saved key" if self.container.credential_store.has_api_key("openrouter") else "not configured"
        )
        hint = Gtk.Label(label=f"OpenRouter status: {source}")
        hint.set_xalign(0)
        hint.add_css_class("muted")
        content.append(hint)

        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_placeholder_text("sk-or-v1-...")
        content.append(entry)

        dialog.connect("response", self._on_auth_dialog_response, entry)
        dialog.present()

    def _on_auth_dialog_response(self, dialog: Gtk.Dialog, response: int, entry: Gtk.Entry) -> None:
        if response == Gtk.ResponseType.OK:
            key = entry.get_text().strip()
            if key:
                self.container.credential_store.set_api_key("openrouter", key)
                self._append_chat_message("Saved OpenRouter API key.", "system")
            else:
                self._append_chat_message("No API key entered.", "error")
        elif response == Gtk.ResponseType.NO:
            self.container.credential_store.delete_api_key("openrouter")
            self._append_chat_message("Removed saved OpenRouter API key.", "system")
        dialog.destroy()

    def _on_stop_clicked(self, _button: Gtk.Button) -> None:
        provider = self.run_state.active_provider
        runtime = self.session.runtimes.get(provider) if provider else None
        if not runtime:
            return

        def stop_runtime() -> None:
            try:
                self._run_coro_in_thread(runtime.manager.stop())
            except Exception as exc:
                GLib.idle_add(self._append_chat_message, f"Stop failed: {exc}", "error")

        threading.Thread(target=stop_runtime, daemon=True).start()
        self._append_chat_message(f"Stop requested for {provider}.", "system")

    def _start_prompt_run(self, prompt: str) -> None:
        provider = normalize_provider_name(self.session.current_provider)
        ready, message = self.container.provider_is_ready(provider)
        if not ready:
            self._append_chat_message(f"{provider}: {message}", "error")
            return
        self._append_chat_message(prompt, "user")
        self._append_chat_message(f"Starting {provider} CLI session...", "system")
        self._set_busy(True, f"Running {provider}")
        self.run_state.active_provider = provider

        def worker() -> None:
            self._run_coro_in_thread(self._run_prompt_async(prompt, provider))

        self.run_state.thread = threading.Thread(target=worker, daemon=True)
        self.run_state.thread.start()

    async def _run_prompt_async(self, prompt: str, provider: str) -> None:
        try:
            runtime = await self.container.ensure_runtime_started(self.session, provider)
            GLib.idle_add(self._append_chat_message, f"{provider} CLI ready. Sending prompt...", "system")

            def stream_event(line: str) -> None:
                GLib.idle_add(self._handle_stream_event, line)

            result = await self.container.execution_service.execute_provider_task(
                session=self.session,
                runtime=runtime,
                provider_name=provider,
                prompt=prompt,
                stream_event_callback=stream_event,
            )
            self.container.remember_task_result(self.session, result)
        except Exception as exc:
            GLib.idle_add(self._append_chat_message, f"Run failed: {exc}", "error")
            GLib.idle_add(self._set_busy, False, "Failed")
            return

        def finish() -> None:
            if result.answer_text.strip():
                self._append_chat_message(result.answer_text.strip(), "assistant")
            elif result.error_text.strip():
                self._append_chat_message(result.error_text.strip(), "error")
            else:
                self._append_chat_message(f"{provider}: completed with exit code {result.exit_code}", "assistant")
            self._update_file_panel_from_result(result)
            if self.session.last_task_run is not None:
                self._update_artifact_panel_from_run(self.session.last_task_run)
            self.open_artifact_button.set_sensitive(self.session.last_task_run is not None)
            self._refresh_session_list()
            self._refresh_run_history()
            self._set_busy(False, "Ready")

        GLib.idle_add(finish)

    def _start_plan_run(self, prompt: str) -> None:
        provider = self.container.pick_planning_provider(self.session)
        ready, message = self.container.provider_is_ready(provider)
        if not ready:
            self._append_chat_message(f"{provider}: {message}", "error")
            return
        self._set_busy(True, f"Planning with {provider}")
        self._append_chat_message(f"Planning: {prompt}", "system")

        def worker() -> None:
            self._run_coro_in_thread(self._run_plan_async(prompt, provider))

        self.run_state.thread = threading.Thread(target=worker, daemon=True)
        self.run_state.thread.start()

    async def _run_plan_async(self, prompt: str, provider: str) -> None:
        try:
            planner = self.container.build_ai_planner(self.session)
            runtime = await self.container.ensure_runtime_started(self.session, provider)
            plan = await planner.build_plan(
                prompt,
                self.container.execution_service,
                self.session,
                runtime,
            )
            self.session.last_plan = plan
            self.container.save_session(self.session)
        except Exception as exc:
            GLib.idle_add(self._append_chat_message, f"Plan failed: {exc}", "error")
            GLib.idle_add(self._set_busy, False, "Failed")
            return

        def finish() -> None:
            self._seed_plan_tasks(plan)
            self._render_plan_preview(plan)
            self._show_inspector("plan")
            self._set_busy(False, "Ready")
            self._refresh_plan_run_controls()
            self._append_chat_message("Plan ready. Accept and run it from the Plan pane.", "system")

        GLib.idle_add(finish)

    def _start_orchestrated_run(self) -> None:
        plan = self.session.last_plan
        if plan is None:
            return
        self._set_busy(True, "Running plan")
        self.run_state.active_provider = self.session.current_provider
        self._seed_plan_tasks(plan)
        self._append_chat_message(f"Running plan: {plan.prompt}", "system")

        def worker() -> None:
            self._run_coro_in_thread(self._run_orchestrated_async())

        self.run_state.thread = threading.Thread(target=worker, daemon=True)
        self.run_state.thread.start()

    async def _run_orchestrated_async(self) -> None:
        plan = self.session.last_plan
        if plan is None:
            GLib.idle_add(self._set_busy, False, "Ready")
            return

        def stream_event(line: str) -> None:
            GLib.idle_add(self._handle_stream_event, line)

        try:
            task_run, result = await self.container.orchestrator_service.run_orchestrated_task(
                session=self.session,
                plan=plan,
                stream_event_callback=stream_event,
            )
        except Exception as exc:
            GLib.idle_add(self._append_chat_message, f"Plan run failed: {exc}", "error")
            GLib.idle_add(self._set_busy, False, "Failed")
            return

        def finish() -> None:
            status = "completed" if result.exit_code == 0 else "failed"
            self._append_chat_message(f"Plan {status}: {task_run.status}", "assistant" if result.exit_code == 0 else "error")
            if task_run.answer_text.strip():
                self._append_chat_message(task_run.answer_text.strip(), "assistant")
            elif task_run.error_text.strip():
                self._append_chat_message(task_run.error_text.strip(), "error")
            self._update_file_panel_from_run(task_run)
            self._update_artifact_panel_from_run(task_run)
            self.open_artifact_button.set_sensitive(True)
            self._refresh_session_list()
            self._refresh_run_history()
            self._set_busy(False, "Ready")

        GLib.idle_add(finish)


class ForgeDesktopApplication(Adw.Application):
    def __init__(self, chat_id: int = 0):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.chat_id = chat_id
        self.container = RuntimeContainer()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_path(str(_css_path()))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = ForgeDesktopWindow(self, self.container, chat_id=self.chat_id)
        window.present()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in {"-h", "--help"} for arg in args):
        print(
            "Usage: forge-desktop\n\n"
            "Repository-local launchers:\n"
            "  ./forge-desktop\n"
            "  python -m desktop.gtk.app\n\n"
            "Install as a local desktop app:\n"
            "  ./scripts/install_desktop.sh\n\n"
            "On Arch/CachyOS, avoid pip --user in externally managed Python.\n"
            "Use the repository-local launcher, pipx, or a venv with --system-site-packages."
        )
        return 0
    app = ForgeDesktopApplication()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
