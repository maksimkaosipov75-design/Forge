import asyncio
import re as _re
import shlex
from pathlib import Path

from cli.command_catalog import grouped_help_lines
from cli.remote_control import RemoteControlManager
from cli.session_actions import (
    build_commit_message,
    clear_session_state,
    compact_session,
    render_todos_lines,
    render_usage_lines,
    run_git_commit,
    run_review_pass,
)
from providers import (
    get_provider_definition,
    is_supported_provider,
    list_provider_models,
    normalize_provider_name,
    provider_default_model,
)

_HTML_TAG = _re.compile(r"<[^>]+>")

def _strip_html(text: str) -> str:
    return _HTML_TAG.sub("", text)


def _action_from_event(line: str) -> str | None:
    """Map a stream event line to a human-readable action label."""
    if line.startswith("🔧 Использую: ") or line.startswith("🔧 "):
        tool = line.split(": ", 1)[-1].strip() if ": " in line else line[2:].strip()
        return (tool[:40] + "…") if len(tool) > 40 else tool + "…"
    if line.startswith(("✏️ ", "📂 ")):
        parts = line[2:].strip().split()
        name = Path(parts[-1]).name if parts else "file"
        return f"Writing {name}…"
    if line.startswith("👁️ "):
        parts = line[2:].strip().split()
        name = Path(parts[-1]).name if parts else "file"
        return f"Reading {name}…"
    if line.startswith("🐚 "):
        cmd = line[2:].strip()
        # strip Russian prefix "Запускаю: " if present
        for prefix in ("Запускаю: ", "Запускаю:", "Running: "):
            if cmd.startswith(prefix):
                cmd = cmd[len(prefix):]
                break
        short = (cmd[:40] + "…") if len(cmd) > 40 else cmd
        return f"$ {short}"
    if line.startswith("⚙️ "):
        return "Initializing…"
    if line.startswith("💬 "):
        return "Writing…"
    return None


class BridgeShell:
    def __init__(self, container, ui, chat_id: int = 0):
        self.container = container
        self.ui = ui
        self.chat_id = chat_id
        self.remote = RemoteControlManager()
        self.running = True
        self.home_visible = False

    async def run(self):
        self.show_home()

        while self.running:
            try:
                session = self.container.get_session(self.chat_id)
                remote_status = self.remote.load_status()
                self.ui.print_input_bar(session.current_provider, remote_status)
                raw = input(
                    self.ui.build_prompt(
                        provider=session.current_provider,
                        remote_status=remote_status,
                        queued=len(session.pending_tasks),
                    )
                ).strip()
            except EOFError:
                self.ui.print_line()
                break
            except KeyboardInterrupt:
                self.ui.print_line()
                break

            if not raw:
                continue

            if raw.startswith("/"):
                await self.handle_slash_command(raw)
                continue

            await self.run_single_task(raw)

    async def handle_slash_command(self, raw: str):
        parts = shlex.split(raw)
        command = parts[0].lower()
        args = parts[1:]

        if command in {"/exit", "/quit"}:
            self.running = False
            return
        if command == "/help":
            self.leave_home_if_needed()
            self.ui.print_shell_help(grouped_help_lines())
            return
        if command == "/commands":
            self.leave_home_if_needed()
            self.ui.print_shell_help(grouped_help_lines())
            return
        if command in {"/home", "/new"}:
            self.show_home()
            return
        if command == "/clear":
            self.leave_home_if_needed()
            session = self.container.get_session(self.chat_id)
            self.ui.print_notice(clear_session_state(session, self.container), kind="success")
            return
        if command == "/compact":
            self.leave_home_if_needed()
            session = self.container.get_session(self.chat_id)
            if args and args[0].isdigit():
                message = compact_session(session, keep=int(args[0]))
            else:
                message = compact_session(session, needle=" ".join(args).strip())
            self.container.save_session(session)
            self.ui.print_notice(message, kind="success")
            return
        if command == "/providers":
            self.leave_home_if_needed()
            for name, path in self.container.provider_paths.items():
                definition = get_provider_definition(name)
                default_model = provider_default_model(name) or "default"
                lines = [
                    f"transport: {definition.transport}",
                    f"default_model: {default_model}",
                    f"specialties: {', '.join(definition.specialties)}",
                    f"target: {path}",
                ]
                if definition.available_models:
                    lines.append(f"models: {len(definition.available_models)} curated")
                self.ui.print_block(f"Provider · {name}", "\n".join(lines), border_style=name)
            return
        if command == "/model":
            self.leave_home_if_needed()
            await self.handle_model(args)
            return
        if command == "/status":
            self.leave_home_if_needed()
            await self.show_status()
            return
        if command == "/limits":
            self.leave_home_if_needed()
            await self.show_limits()
            return
        if command == "/usage":
            self.leave_home_if_needed()
            session = self.container.get_session(self.chat_id)
            self.ui.print_block("Usage", "\n".join(render_usage_lines(session, self.container.provider_paths)), border_style="cyan")
            return
        if command == "/metrics":
            self.leave_home_if_needed()
            self.ui.print_block("Metrics", self.container.metrics.render_prometheus(), border_style="cyan")
            return
        if command == "/todos":
            self.leave_home_if_needed()
            session = self.container.get_session(self.chat_id)
            self.ui.print_block("TODOs", "\n".join(render_todos_lines(session)), border_style="yellow")
            return
        if command == "/review":
            self.leave_home_if_needed()
            session = self.container.get_session(self.chat_id)
            ok, provider_or_message, output = await run_review_pass(self.container, session, " ".join(args).strip())
            if ok:
                self.ui.print_block(f"Review · {provider_or_message}", output[:6000] or "Empty review.", border_style="yellow")
            else:
                detail = output or provider_or_message
                self.ui.print_notice(detail, kind="error")
            return
        if command == "/commit":
            self.leave_home_if_needed()
            session = self.container.get_session(self.chat_id)
            message = build_commit_message(session, " ".join(args).strip())
            ok, output = run_git_commit(str(session.file_mgr.get_working_dir()), message)
            self.ui.print_notice(output, kind="success" if ok else "warning")
            return
        if command == "/provider":
            self.leave_home_if_needed()
            await self.set_provider(args)
            return
        if command == "/plan":
            self.leave_home_if_needed()
            await self.plan_prompt(" ".join(args).strip())
            return
        if command == "/run-plan":
            self.leave_home_if_needed()
            session = self.container.get_session(self.chat_id)
            if session.last_plan is None:
                self.ui.print_notice("No saved plan. Use /plan <task> first.", kind="warning")
                return
            await self._run_prebuilt_plan(session.last_plan)
            return
        if command == "/orchestrate":
            self.leave_home_if_needed()
            await self.orchestrate_prompt(" ".join(args).strip())
            return
        if command == "/runs":
            self.leave_home_if_needed()
            await self.show_runs()
            return
        if command == "/show":
            self.leave_home_if_needed()
            await self.show_run(args)
            return
        if command == "/artifacts":
            self.leave_home_if_needed()
            await self.show_artifacts()
            return
        if command == "/remote-control":
            self.leave_home_if_needed()
            await self.handle_remote_control(args)
            return

        self.leave_home_if_needed()
        self.ui.print_line(f"Unknown command: {command}")
        self.ui.print_line("Use /help to list available commands.")

    def show_home(self):
        session = self.container.get_session(self.chat_id)
        self.ui.print_home(
            session,
            self.container.recent_runs(session, limit=5),
            self.remote.load_status(),
        )
        self.home_visible = True

    def leave_home_if_needed(self):
        if not self.home_visible:
            return
        self.home_visible = False

    async def set_provider(self, args: list[str]):
        session = self.container.get_session(self.chat_id)
        if not args:
            self.ui.print_kv("provider", session.current_provider)
            return
        provider = normalize_provider_name(args[0])
        if not is_supported_provider(provider):
            self.ui.print_notice(f"Unsupported provider: {args[0]}", kind="error")
            return
        session.current_provider = provider
        self.container.save_session(session)
        self.ui.print_notice(f"Default provider set to {provider}.", provider=provider, kind="success")

    async def handle_model(self, args: list[str]):
        session = self.container.get_session(self.chat_id)

        if not args:
            for provider_name in self.container.provider_paths:
                self._print_model_block(session, provider_name)
            return

        provider_name = normalize_provider_name(args[0])
        if provider_name not in self.container.provider_paths:
            provider_name = session.current_provider
            new_model = " ".join(args).strip()
        else:
            new_model = " ".join(args[1:]).strip()

        if provider_name not in self.container.provider_paths:
            self.ui.print_notice(f"Unsupported provider: {args[0]}", kind="error")
            return

        if not new_model:
            self._print_model_block(session, provider_name)
            return

        if new_model.lower() == "default":
            new_model = ""

        session.provider_models[provider_name] = new_model
        self.container.reset_runtime(session, provider_name)
        self.container.save_session(session)
        label = new_model or provider_default_model(provider_name) or "default"
        self.ui.print_notice(
            f"{provider_name} model set to {label}. The new model will be used on the next prompt.",
            provider=provider_name,
            kind="success",
        )

    def _print_model_block(self, session, provider_name: str):
        current = session.provider_models.get(provider_name, "").strip()
        resolved = current or provider_default_model(provider_name) or "default"
        lines = [f"current: {resolved}"]
        catalog = list_provider_models(provider_name)
        if catalog:
            lines.append("")
            lines.append("available:")
            for item in catalog:
                marker = "*" if item.name == current else "-"
                lines.append(f"  {marker} {item.name}  {item.label}")
        self.ui.print_block(f"Model · {provider_name}", "\n".join(lines), border_style=provider_name)

    async def show_status(self):
        session = self.container.get_session(self.chat_id)
        self.ui.print_session_status(session, self.remote.load_status())

    async def show_limits(self):
        session = self.container.get_session(self.chat_id)
        provider_lines: list[str] = []
        for provider_name in self.container.provider_paths:
            runtime = session.runtimes.get(provider_name)
            if runtime is None or runtime.health is None:
                provider_lines.append(f"{provider_name}\navailability: unknown\ncontext: unknown")
                continue
            health = runtime.health
            lines = [
                provider_name,
                f"availability: {'available' if health.available else 'limited or failing'}",
                f"context: {health.context_status}",
            ]
            if health.last_failure:
                lines.append(f"failure: {health.last_failure.short_label}")
                lines.append(f"reason: {health.last_failure.message}")
                if health.last_failure.retry_at:
                    lines.append(f"retry_at: {health.last_failure.retry_at}")
            elif health.last_limit_message:
                lines.append(f"last_limit: {health.last_limit_message}")
            provider_lines.append("\n".join(lines))
        self.ui.print_provider_limits(provider_lines)

    async def plan_prompt(self, prompt: str):
        if not prompt:
            self.ui.print_notice("Usage: /plan <task>", kind="warning")
            return
        session = self.container.get_session(self.chat_id)
        plan = await self._build_plan(session, prompt)
        session.last_plan = plan
        self.container.save_session(session)
        self.ui.print_plan(plan)
        self.ui.print_shell_footer()

    async def run_single_task(self, prompt: str):
        self.leave_home_if_needed()
        session = self.container.get_session(self.chat_id)
        provider_name = normalize_provider_name(session.current_provider)
        runtime = await self.container.ensure_runtime_started(session, provider_name)
        cwd = str(session.file_mgr.get_working_dir())

        self.ui.print_task_header(provider_name, cwd, prompt)

        live, start_time, state = self.ui.start_status_bar(provider_name)
        task_done = asyncio.Event()

        async def tick():
            while not task_done.is_set():
                await asyncio.sleep(0.5)
                self.ui.refresh_status_bar(live, start_time, state, provider_name)

        timer = asyncio.create_task(tick())

        def stream_event_callback(line: str):
            action = _action_from_event(line)
            if action:
                state["action"] = action
            if line.startswith("💬 "):
                state["tokens"] += max(1, len(line[2:]) // 4)
            self.ui.print_stream_event(line, provider_name)
            self.ui.refresh_status_bar(live, start_time, state, provider_name)

        try:
            result = await self.container.execution_service.execute_provider_task(
                session=session,
                runtime=runtime,
                provider_name=provider_name,
                prompt=prompt,
                stream_event_callback=stream_event_callback,
            )
        finally:
            task_done.set()
            try:
                await asyncio.wait_for(timer, timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self.ui.stop_status_bar(live)

        self.container.remember_task_result(session, result)
        self.ui.print_task_result_inline(result)
        self.home_visible = False

    async def _build_plan(self, session, prompt: str):
        """Try AI planner; fall back to rule-based silently."""
        planner = self.container.build_ai_planner(session)
        planning_provider = self.container.pick_planning_provider(session)
        planning_runtime = await self.container.ensure_runtime_started(session, planning_provider)
        return await planner.build_plan(
            prompt,
            self.container.execution_service,
            session,
            planning_runtime,
        )

    async def orchestrate_prompt(self, prompt: str):
        if not prompt:
            self.ui.print_notice("Usage: /orchestrate <task>", kind="warning")
            return

        self.leave_home_if_needed()
        session = self.container.get_session(self.chat_id)
        plan = await self._build_plan(session, prompt)
        session.last_plan = plan
        self.container.save_session(session)
        await self._run_prebuilt_plan(plan)

    async def _run_prebuilt_plan(self, plan):
        session = self.container.get_session(self.chat_id)
        cwd = str(session.file_mgr.get_working_dir())

        # Print plan header
        self.ui.print_task_header(session.current_provider, cwd, f"[orchestrate] {plan.prompt}")
        for index, item in enumerate(plan.subtasks, start=1):
            self.ui.print_line(
                f"  [dim]{index}. {item.title} [{item.suggested_provider}][/dim]"
                if self.ui.console else f"  {index}. {item.title} [{item.suggested_provider}]"
            )
        self.ui.print_line()

        current_step = {"index": -1}
        live, start_time, state = self.ui.start_status_bar(session.current_provider)
        orch_done = asyncio.Event()

        async def tick():
            while not orch_done.is_set():
                await asyncio.sleep(0.5)
                active = session.active_provider or session.current_provider
                self.ui.refresh_status_bar(live, start_time, state, active)

        timer = asyncio.create_task(tick())

        async def status_callback(text: str):
            clean = _strip_html(text).strip()
            if not clean:
                return
            lowered = clean.lower()
            if "шаг " in lowered and "агент:" in lowered:
                next_index = current_step["index"] + 1
                if next_index < len(plan.subtasks):
                    subtask = plan.subtasks[next_index]
                    self.ui.print_orchestration_step_header(
                        next_index + 1, len(plan.subtasks),
                        subtask.title, subtask.suggested_provider, cwd
                    )
                    state["action"] = f"Step {next_index + 1}/{len(plan.subtasks)}…"
                    state["tokens"] = 0
                    start_time_ref[0] = __import__("time").monotonic()
                    current_step["index"] = next_index
            elif "собирает итог" in lowered:
                active = session.active_provider or session.current_provider
                self.ui.print_orchestration_label("Synthesis", active, cwd)
                state["action"] = "Synthesizing…"
                state["tokens"] = 0
                start_time_ref[0] = __import__("time").monotonic()
            elif "выполняет review" in lowered:
                active = session.active_provider or session.current_provider
                self.ui.print_orchestration_label("Review", active, cwd)
                state["action"] = "Reviewing…"
                state["tokens"] = 0
                start_time_ref[0] = __import__("time").monotonic()

        start_time_ref = [start_time]

        def stream_event_callback(line: str):
            action = _action_from_event(line)
            if action:
                state["action"] = action
            if line.startswith("💬 "):
                state["tokens"] += max(1, len(line[2:]) // 4)
            active = session.active_provider or session.current_provider
            self.ui.print_stream_event(line, active)
            self.ui.refresh_status_bar(live, start_time_ref[0], state, active)

        try:
            task_run, aggregate_result = await self.container.orchestrator_service.run_orchestrated_task(
                session=session,
                plan=plan,
                status_callback=status_callback,
                stream_event_callback=stream_event_callback,
            )
        finally:
            orch_done.set()
            try:
                await asyncio.wait_for(timer, timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self.ui.stop_status_bar(live)

        # Per-subtask results
        self.ui.print_line()
        for subtask in task_run.subtasks:
            self.ui.print_orchestration_subtask_result(subtask)

        # Final answer
        if aggregate_result.answer_text.strip():
            self.ui.print_line()
            self.ui.print_line(aggregate_result.answer_text.strip()[:4000])

        self.ui.print_line()
        status_icon = "✓" if task_run.status == "success" else "✗"
        self.ui.print_notice(
            f"{status_icon} Orchestration {task_run.status}  {task_run.duration_text}",
            kind="success" if task_run.status == "success" else "error",
        )
        self.home_visible = False

    async def show_runs(self):
        session = self.container.get_session(self.chat_id)
        runs = self.container.recent_runs(session, limit=10)
        if not runs:
            self.ui.print_notice("No runs yet.", kind="warning")
            return
        for index, run in enumerate(runs, start=1):
            self.ui.print_run_brief(run, index=index)

    async def show_run(self, args: list[str]):
        if not args:
            self.ui.print_notice("Usage: /show <index>", kind="warning")
            return
        try:
            index = int(args[0])
        except ValueError:
            self.ui.print_notice("Run index must be a number.", kind="error")
            return
        session = self.container.get_session(self.chat_id)
        run = self.container.run_by_index(session, index)
        if run is None:
            self.ui.print_notice(f"Run {index} not found.", kind="error")
            return
        self.ui.print_run_detail(run)

    async def show_artifacts(self):
        session = self.container.get_session(self.chat_id)
        self.ui.print_artifacts(self.container.latest_artifact_files(session))

    async def handle_remote_control(self, args: list[str]):
        action = args[0].lower() if args else "start"
        if action == "start":
            try:
                status = self.remote.start()
            except RuntimeError as exc:
                self.ui.print_notice(str(exc), kind="error")
                return
            self.ui.print_remote_status(status, message="Telegram remote control started.")
            return
        if action == "status":
            self.ui.print_remote_status(self.remote.load_status())
            return
        if action == "stop":
            status = self.remote.stop()
            self.ui.print_remote_status(status, message="Telegram remote control stopped.")
            return
        if action == "logs":
            logs = self.remote.tail_logs()
            if not logs:
                self.ui.print_notice("No remote-control logs yet.", kind="warning")
                return
            self.ui.print_block("Remote Control Logs", logs)
            return

        self.ui.print_notice("Usage: /remote-control [status|stop|logs]", kind="warning")


async def run_shell(container, ui, chat_id: int = 0):
    shell = BridgeShell(container, ui, chat_id=chat_id)
    await shell.run()
