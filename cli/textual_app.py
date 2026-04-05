from __future__ import annotations

import asyncio
import re as _re
from pathlib import Path

_HTML_TAG = _re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG.sub("", text)


def run_textual_shell(container, chat_id: int = 0):
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Container, Horizontal
        from textual.reactive import reactive
        from textual.suggester import Suggester
        from textual.widget import Widget
        from textual.widgets import Input, Static
    except ImportError as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError(
            "Textual mode requires the 'textual' package. Install it with './venv/bin/pip install textual'."
        ) from exc

    COMMANDS: dict[str, tuple[str, str]] = {
        "/help": ("Shell", "Show available commands"),
        "/home": ("Shell", "Return to the start page"),
        "/new": ("Shell", "Reset the shell workspace"),
        "/provider": ("Providers", "Switch the default provider"),
        "/providers": ("Providers", "List available providers"),
        "/status": ("Status", "Show current shell/session status"),
        "/limits": ("Status", "Show provider health and limits"),
        "/runs": ("History", "List recent runs"),
        "/show": ("History", "Show details for a run by index"),
        "/artifacts": ("History", "List latest artifact files"),
        "/plan": ("Orchestration", "Preview an orchestration plan"),
        "/orchestrate": ("Orchestration", "Run a multi-agent orchestration"),
        "/remote-control": ("Remote", "Start or manage Telegram remote access"),
        "/quit": ("Shell", "Exit the textual shell"),
        "/exit": ("Shell", "Exit the textual shell"),
    }

    class SlashCommandSuggester(Suggester):
        def __init__(self):
            super().__init__(case_sensitive=False)
            self.commands = list(COMMANDS.keys())

        async def get_suggestion(self, value: str) -> str | None:
            if not value.startswith("/"):
                return None
            if " " in value.strip():
                return None
            lowered = value.casefold()
            for command in self.commands:
                if command.casefold().startswith(lowered):
                    return command
            return None

    class TitleWidget(Static):
        pass

    class StreamWidget(Static):
        pass

    class SideWidget(Static):
        pass

    class BridgeTextualApp(App):
        CSS = """
        Screen {
            background: #111318;
            color: #f3f3f3;
        }

        #titlebar {
            height: 1;
            padding: 0 1;
            background: #1a1d25;
            color: #9aa3b2;
        }

        #workspace {
            margin: 0 1;
            height: 1fr;
        }

        #stream {
            border: round #6aa7ff;
            padding: 0 1;
            height: 1fr;
            width: 1fr;
        }

        #side {
            border: round #ff9e57;
            padding: 0 1;
            width: 34;
        }

        #input {
            dock: bottom;
            margin: 0 1 0 1;
        }
        """

        current_provider = reactive("qwen")
        remote_state = reactive("stopped")
        current_mode = reactive("idle")
        current_input = reactive("")

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.timeline_entries: list[str] = ["Shell ready."]
            self.orchestration_steps: list[str] = []

        def _provider_color(self) -> str:
            mapping = {
                "qwen": "#b07cff",
                "codex": "#6aa7ff",
                "claude": "#ff9e57",
            }
            return mapping.get(self.current_provider, "#6aa7ff")

        def compose(self) -> ComposeResult:
            session = container.get_session(chat_id)
            self.current_provider = session.current_provider
            yield TitleWidget(self._titlebar_text(), id="titlebar")
            with Container(id="workspace"):
                with Horizontal():
                    yield StreamWidget("Ready.", id="stream")
                    yield SideWidget(self._side_text(), id="side")
            yield Input(
                placeholder="/help · /provider <name> · /orchestrate <task> · /remote-control",
                id="input",
                suggester=SlashCommandSuggester(),
            )

        def on_mount(self):
            self._sync_remote_state()
            self._apply_provider_theme()
            self._refresh_all()

        def _titlebar_text(self) -> str:
            session = container.get_session(chat_id)
            cwd = str(session.file_mgr.get_working_dir())
            specialties = {"qwen": "python·data", "codex": "backend·refactor", "claude": "ui·writing"}
            spec = specialties.get(self.current_provider, "general")
            return (
                f">_ {self.current_provider.upper()} [{spec}]  "
                f"{cwd}  "
                f"mode:{self.current_mode}  remote:{self.remote_state}"
            )

        def _provider_specialties(self) -> str:
            mapping = {
                "qwen": "python · scripting · data",
                "codex": "systems · backend · refactor",
                "claude": "ui · ux · writing",
            }
            return mapping.get(self.current_provider, "general purpose")

        def _side_text(self) -> str:
            session = container.get_session(chat_id)
            recent = container.recent_runs(session, limit=5)
            if not recent:
                recent_lines = ["No recent runs yet."]
            else:
                recent_lines = [
                    f"{index}. {run.status_emoji} {run.mode} [{run.provider_summary or 'mixed'}]"
                    for index, run in enumerate(recent, start=1)
                ]
            provider_lines = []
            for provider_name in container.provider_paths:
                runtime = session.runtimes.get(provider_name)
                if runtime is None or runtime.health is None:
                    provider_lines.append(f"{provider_name}: unknown")
                    continue
                health = runtime.health
                state = "up" if health.available else "limited"
                if health.last_failure:
                    state += f" · {health.last_failure.short_label}"
                    if health.last_failure.retry_at:
                        state += f" @ {health.last_failure.retry_at}"
                provider_lines.append(f"{provider_name}: {state}")
            command_hint_lines = self._command_hint_lines()
            return "\n".join(
                [
                    "Remote",
                    f"state: {self.remote_state}",
                    "",
                    "Providers",
                    *provider_lines,
                    "",
                    "Orchestration",
                    *(self.orchestration_steps[-8:] or ["No active plan."]),
                    "",
                    "Timeline",
                    *self.timeline_entries[-8:],
                    "",
                    "Command hints",
                    *command_hint_lines,
                    "",
                    "Recent activity",
                    *recent_lines,
                    "",
                    "Tips",
                    "/new resets the screen",
                    "/plan previews orchestration",
                    "/orchestrate runs multi-agent mode",
                    "/remote-control starts Telegram access",
                ]
            )

        def _command_hint_lines(self) -> list[str]:
            value = self.current_input.strip()
            if not value.startswith("/"):
                return ["Type / to see command suggestions."]
            command_part = value.split(maxsplit=1)[0]
            matches = [
                f"{name} · {meta[0]} · {meta[1]}"
                for name, meta in COMMANDS.items()
                if name.startswith(command_part)
            ]
            return matches[:6] or ["No matching slash commands."]

        def _statusbar_text(self) -> str:
            # kept for compatibility but not rendered
            session = container.get_session(chat_id)
            return " · ".join(
                [
                    f"provider: {self.current_provider}",
                    f"remote: {self.remote_state}",
                    f"mode: {self.current_mode}",
                    f"runs: {len(session.run_history)}",
                ]
            )

        def _sync_remote_state(self):
            from cli.remote_control import RemoteControlManager

            status = RemoteControlManager().load_status()
            self.remote_state = "running" if status.is_running else "stopped"

        def _apply_provider_theme(self):
            color = self._provider_color()
            self.query_one("#stream", StreamWidget).styles.border = ("round", color)
            self.query_one("#input", Input).styles.border = ("round", color)

        def _refresh_all(self):
            self.query_one("#titlebar", TitleWidget).update(self._titlebar_text())
            self.query_one("#side", SideWidget).update(self._side_text())

        def _append_stream(self, *lines: str):
            stream = self.query_one("#stream", StreamWidget)
            current = str(stream.renderable or "").splitlines()
            current.extend(line for line in lines if line is not None)
            stream.update("\n".join(current[-30:]))

        def _add_timeline(self, message: str):
            self.timeline_entries.append(message)
            self.timeline_entries = self.timeline_entries[-20:]
            self.query_one("#side", SideWidget).update(self._side_text())

        def _set_orchestration_steps(self, steps: list[str]):
            self.orchestration_steps = steps[-20:]
            self.query_one("#side", SideWidget).update(self._side_text())

        def _mark_orchestration_step(self, step_index: int, state: str):
            if step_index < 0 or step_index >= len(self.orchestration_steps):
                return
            current = self.orchestration_steps[step_index]
            suffix = current.split("] ", 1)[1] if "] " in current else current
            self.orchestration_steps[step_index] = f"[{state}] {suffix}"
            self.query_one("#side", SideWidget).update(self._side_text())

        def action_show_help(self) -> None:
            asyncio.create_task(self._handle_command("/help"))

        async def on_key(self, event) -> None:
            if event.key == "?":
                await self._handle_command("/help")

        async def on_input_changed(self, event: Input.Changed) -> None:
            self.current_input = event.value
            self.query_one("#side", SideWidget).update(self._side_text())

        async def on_input_submitted(self, event: Input.Submitted):
            value = event.value.strip()
            event.input.value = ""
            self.current_input = ""
            self.query_one("#side", SideWidget).update(self._side_text())
            if not value:
                return

            suggestion = getattr(event.input, "_suggestion", "")
            if value.startswith("/") and " " not in value and suggestion and suggestion != value:
                value = suggestion

            if value.startswith("/"):
                await self._handle_command(value)
                return

            await self._run_prompt(value)

        async def _handle_command(self, raw: str):
            stream = self.query_one("#stream", StreamWidget)
            session = container.get_session(chat_id)
            parts = raw.split(maxsplit=1)
            command = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if command in {"/quit", "/exit"}:
                self.exit()
                return
            if command in {"/home", "/new"}:
                self.current_mode = "idle"
                self._set_orchestration_steps([])
                self._add_timeline("Workspace reset.")
                stream.update("Ready.")
                self._refresh_all()
                return
            if command == "/help":
                self._add_timeline("Opened help.")
                stream.update(
                    "\n".join(
                        [
                            "/help",
                            "/home",
                            "/new",
                            "/provider <qwen|codex|claude>",
                            "/status",
                            "/limits",
                            "/runs",
                            "/remote-control",
                            "/remote-control status",
                            "/remote-control stop",
                            "/plan <task>",
                            "/orchestrate <task>",
                        ]
                    )
                )
                return
            if command == "/provider":
                if not arg:
                    stream.update(f"provider: {session.current_provider}")
                    return
                from providers import is_supported_provider, normalize_provider_name

                provider = normalize_provider_name(arg)
                if not is_supported_provider(provider):
                    stream.update(f"Unsupported provider: {arg}")
                    return
                session.current_provider = provider
                container.save_session(session)
                self.current_provider = provider
                self._apply_provider_theme()
                self._add_timeline(f"Provider set to {provider}.")
                self._refresh_all()
                stream.update(f"Default provider set to {provider}.")
                return
            if command == "/providers":
                stream.update(
                    "\n".join(
                        f"{name} · {path}"
                        for name, path in container.provider_paths.items()
                    )
                )
                return
            if command == "/status":
                self._add_timeline("Viewed status.")
                stream.update(
                    "\n".join(
                        [
                            f"provider: {session.current_provider}",
                            f"working_dir: {session.file_mgr.get_working_dir()}",
                            f"runs: {len(session.run_history)}",
                            f"remote: {self.remote_state}",
                        ]
                    )
                )
                return
            if command == "/limits":
                self._add_timeline("Viewed limits.")
                provider_lines: list[str] = []
                for provider_name in container.provider_paths:
                    runtime = session.runtimes.get(provider_name)
                    if runtime is None or runtime.health is None:
                        provider_lines.append(f"{provider_name}: availability unknown · context unknown")
                        continue
                    health = runtime.health
                    line = f"{provider_name}: {'available' if health.available else 'limited'} · context {health.context_status}"
                    if health.last_failure:
                        line += f" · {health.last_failure.short_label}"
                        if health.last_failure.retry_at:
                            line += f" · retry {health.last_failure.retry_at}"
                    provider_lines.append(line)
                stream.update("\n".join(provider_lines))
                return
            if command == "/runs":
                self._add_timeline("Viewed runs.")
                runs = container.recent_runs(session, limit=10)
                if not runs:
                    stream.update("No runs yet.")
                    return
                stream.update(
                    "\n".join(
                        f"{index}. {run.status_emoji} {run.mode} [{run.provider_summary or 'mixed'}]"
                        for index, run in enumerate(runs, start=1)
                    )
                )
                return
            if command == "/show":
                if not arg:
                    stream.update("Usage: /show <index>")
                    return
                try:
                    index = int(arg)
                except ValueError:
                    stream.update("Run index must be a number.")
                    return
                run = container.run_by_index(session, index)
                if run is None:
                    stream.update(f"Run {index} not found.")
                    return
                details = [
                    f"run_id: {run.run_id}",
                    f"status: {run.status}",
                    f"mode: {run.mode}",
                    f"complexity: {run.complexity}",
                ]
                if run.strategy:
                    details.append(f"strategy: {run.strategy}")
                if run.provider_summary:
                    details.append(f"providers: {run.provider_summary}")
                if run.artifact_file:
                    details.append(f"artifact: {run.artifact_file}")
                if run.subtasks:
                    details.append("")
                    details.append("subtasks:")
                    details.extend(
                        f"- {item.subtask_id}: {item.title} [{item.provider}] ({item.status})"
                        for item in run.subtasks
                    )
                stream.update("\n".join(details))
                return
            if command == "/artifacts":
                artifacts = container.latest_artifact_files(session)
                if not artifacts:
                    stream.update("No artifacts found.")
                    return
                stream.update("\n".join(str(item) for item in artifacts))
                return
            if command == "/plan":
                if not arg:
                    stream.update("Usage: /plan <task>")
                    return
                plan = container.build_planner(session).build_plan(arg)
                session.last_plan = plan
                container.save_session(session)
                self._add_timeline(f"Planned: {arg[:48]}")
                stream.update(
                    "\n".join(
                        [
                            f"complexity: {plan.complexity}",
                            f"strategy: {plan.strategy}",
                            "",
                            *[
                                f"{index}. {item.title} [{item.suggested_provider}]"
                                for index, item in enumerate(plan.subtasks, start=1)
                            ],
                        ]
                    )
                )
                return
            if command == "/orchestrate":
                if not arg:
                    stream.update("Usage: /orchestrate <task>")
                    return
                await self._run_orchestration(arg)
                return
            if command == "/remote-control":
                from cli.remote_control import RemoteControlManager

                manager = RemoteControlManager()
                action = arg or "start"
                if action == "start":
                    try:
                        status = manager.start()
                    except RuntimeError as exc:
                        stream.update(str(exc))
                        return
                    self.remote_state = "running" if status.is_running else "stopped"
                    self._add_timeline("Remote control started.")
                    self._refresh_all()
                    stream.update(f"Remote control started. log: {status.log_path}")
                    return
                if action == "status":
                    status = manager.load_status()
                    self.remote_state = "running" if status.is_running else "stopped"
                    self._add_timeline("Checked remote-control status.")
                    self._refresh_all()
                    stream.update(
                        "\n".join(
                            [
                                f"running: {'yes' if status.is_running else 'no'}",
                                f"pid: {status.pid or '-'}",
                                f"log_path: {status.log_path or '-'}",
                            ]
                        )
                    )
                    return
                if action == "stop":
                    status = manager.stop()
                    self.remote_state = "running" if status.is_running else "stopped"
                    self._add_timeline("Remote control stopped.")
                    self._refresh_all()
                    stream.update("Remote control stopped.")
                    return
                if action == "logs":
                    logs = manager.tail_logs()
                    stream.update(logs or "No remote-control logs yet.")
                    return
                stream.update("Usage: /remote-control [status|stop|logs]")
                return

            stream.update(f"Unknown command: {raw}")

        async def _run_prompt(self, prompt: str):
            from providers import normalize_provider_name

            session = container.get_session(chat_id)
            provider_name = normalize_provider_name(session.current_provider)
            runtime = await container.ensure_runtime_started(session, provider_name)
            stream = self.query_one("#stream", StreamWidget)
            self.current_mode = "single"
            self._set_orchestration_steps([])
            self._add_timeline(f"Started single task via {provider_name}.")
            self._refresh_all()
            stream_lines: list[str] = [f"Running with {provider_name}..."]

            async def status_callback(text: str):
                if text:
                    clean = _strip_html(text).strip()
                    for line in clean.splitlines():
                        line = line.strip()
                        if line:
                            stream_lines.append(line)
                    self._add_timeline(clean[:72])
                    stream.update("\n".join(stream_lines[-25:]))

            result = await container.execution_service.execute_provider_task(
                session=session,
                runtime=runtime,
                provider_name=provider_name,
                prompt=prompt,
                status_callback=status_callback,
            )
            container.remember_task_result(session, result)
            self.current_mode = "idle"
            self._add_timeline(f"Finished single task exit_code={result.exit_code}.")
            self._refresh_all()
            stream.update(
                "\n".join(
                    [
                        *stream_lines[-10:],
                        "",
                        f"exit_code: {result.exit_code}",
                        result.answer_text[:3000],
                    ]
                )
            )

        async def _run_orchestration(self, prompt: str):
            session = container.get_session(chat_id)
            stream = self.query_one("#stream", StreamWidget)
            self.current_mode = "orchestrated"
            self._add_timeline("Started orchestration.")
            self._refresh_all()
            plan = container.build_planner(session).build_plan(prompt)
            session.last_plan = plan
            container.save_session(session)
            self._set_orchestration_steps(
                [
                    *[
                        f"[pending] {index}. {item.title} [{item.suggested_provider}]"
                        for index, item in enumerate(plan.subtasks, start=1)
                    ],
                    "[pending] synthesis",
                    "[pending] review",
                ]
            )
            cwd = str(session.file_mgr.get_working_dir())
            stream_lines: list[str] = [
                f"strategy: {plan.strategy}",
                f"cwd: {cwd}",
                *[
                    f"  {index}. {item.title} [{item.suggested_provider}]"
                    for index, item in enumerate(plan.subtasks, start=1)
                ],
                "",
                "Starting orchestration...",
            ]
            stream.update("\n".join(stream_lines))
            current_step = {"index": -1}

            async def status_callback(text: str):
                if not text:
                    return
                clean = _strip_html(text).strip()
                lowered = clean.lower()
                if "шаг " in lowered and "агент:" in lowered:
                    next_index = min(current_step["index"] + 1, max(0, len(plan.subtasks) - 1))
                    if current_step["index"] >= 0:
                        self._mark_orchestration_step(current_step["index"], "done")
                    current_step["index"] = next_index
                    self._mark_orchestration_step(current_step["index"], "running")
                    # Emit step header with folder context
                    stream_lines.append("")
                    stream_lines.append(f"{'─' * 40}")
                    stream_lines.append(clean)
                    stream_lines.append(f"folder: {cwd}")
                elif "собирает итог" in lowered:
                    if 0 <= current_step["index"] < len(plan.subtasks):
                        self._mark_orchestration_step(current_step["index"], "done")
                    self._mark_orchestration_step(len(plan.subtasks), "running")
                    stream_lines.append("")
                    stream_lines.append(f"{'─' * 40}")
                    stream_lines.append(clean)
                elif "выполняет review" in lowered:
                    self._mark_orchestration_step(len(plan.subtasks), "done")
                    self._mark_orchestration_step(len(plan.subtasks) + 1, "running")
                    stream_lines.append("")
                    stream_lines.append(f"{'─' * 40}")
                    stream_lines.append(clean)
                else:
                    # Model commentary / tool activity lines
                    for line in clean.splitlines():
                        line = line.strip()
                        if line:
                            stream_lines.append(line)
                self._add_timeline(clean[:72])
                stream.update("\n".join(stream_lines[-30:]))

            task_run, aggregate_result = await container.orchestrator_service.run_orchestrated_task(
                session=session,
                plan=plan,
                status_callback=status_callback,
            )
            self.current_mode = "idle"
            if 0 <= current_step["index"] < len(plan.subtasks):
                self._mark_orchestration_step(current_step["index"], "done")
            self._mark_orchestration_step(len(plan.subtasks), "done")
            self._mark_orchestration_step(
                len(plan.subtasks) + 1,
                "done" if task_run.review_answer else "skipped",
            )
            self._add_timeline(f"Finished orchestration status={task_run.status}.")
            self._refresh_all()

            # Build final summary with per-subtask file changes and model answers
            final_lines: list[str] = [
                f"{'═' * 40}",
                f"status: {task_run.status}  cwd: {cwd}",
                "",
            ]
            for subtask in task_run.subtasks:
                status_icon = "✓" if subtask.status == "success" else "✗"
                final_lines.append(f"{status_icon} [{subtask.provider}] {subtask.title}")
                if subtask.new_files:
                    final_lines.append("  + " + "  + ".join(Path(f).name for f in subtask.new_files[:6]))
                if subtask.changed_files:
                    final_lines.append("  ~ " + "  ~ ".join(Path(f).name for f in subtask.changed_files[:6]))
                if subtask.answer_text.strip():
                    excerpt = subtask.answer_text.strip()[:300].replace("\n", " ")
                    final_lines.append(f"  > {excerpt}")
                final_lines.append("")

            if task_run.review_answer:
                final_lines.append("Review:")
                final_lines.append(task_run.review_answer.strip()[:600])
            elif aggregate_result.answer_text.strip():
                final_lines.append("Result:")
                final_lines.append(aggregate_result.answer_text.strip()[:1800])

            stream.update("\n".join(final_lines))

    BridgeTextualApp().run()
