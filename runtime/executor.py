import asyncio
import logging
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable

from core.providers import is_api_provider, provider_transport
from core.task_models import ProviderRuntime, TaskResult, utc_now_iso


log = logging.getLogger(__name__)


StatusCallback = Callable[[str], Awaitable[None]]
StatusFormatter = Callable[[str], str]

_HISTORY_WINDOW = 6  # past turn pairs to send to API providers


def _configure_api_manager(manager, session, current_prompt: str) -> None:
    """Populate thinking_enabled and conversation_history on an API backend before use."""
    thinking_mode = (session.ui_preferences or {}).get("thinking_mode", "compact").strip().lower()
    manager.thinking_enabled = thinking_mode in ("compact", "full")

    messages: list[dict] = []
    for result in session.history[-_HISTORY_WINDOW:]:
        user_text = (result.prompt or "").strip()
        assistant_text = (result.answer_text or "").strip()
        if user_text:
            messages.append({"role": "user", "content": user_text})
        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
    manager.conversation_history = messages


# Directories whose contents are never authored by the user / AI agents.
# Changes in these dirs during a task are noise (compilation, IDE indexing, …).
_SCAN_SKIP_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".tox", ".nox", ".hypothesis",
    "venv", ".venv", "env", ".env", "virtualenv",
    "node_modules", ".yarn", ".pnp",
    ".git", ".hg", ".svn", ".bzr",
    "dist", "build", "_build", "target",   # Rust / Maven
    ".gradle", ".idea", ".vscode", ".vs",
    ".eggs", "*.egg-info",
})


class ExecutionService:
    @staticmethod
    def _scan_dir_sync(directory: Path) -> dict[str, float]:
        result = {}
        try:
            for item in directory.rglob("*"):
                parts = item.parts
                # Skip hidden paths and known non-project directories.
                if any(
                    part.startswith(".") or part in _SCAN_SKIP_DIRS
                    for part in parts
                ):
                    continue
                if item.is_file():
                    try:
                        result[str(item)] = item.stat().st_mtime
                    except OSError:
                        pass
        except PermissionError:
            pass
        return result

    async def scan_dir(self, directory: Path) -> dict[str, float]:
        return await asyncio.get_running_loop().run_in_executor(None, self._scan_dir_sync, directory)

    async def execute_provider_task(
        self,
        session,
        runtime: ProviderRuntime,
        provider_name: str,
        prompt: str,
        status_callback: StatusCallback | None = None,
        status_prefix: str | None = None,
        status_formatter: StatusFormatter | None = None,
        stream_event_callback: Callable[[str], None] | None = None,
        interaction_callback: Callable[[str, str], Awaitable[str | None]] | None = None,
    ) -> TaskResult:
        work_dir = session.file_mgr.get_working_dir()
        session.last_task_result = TaskResult(provider=provider_name, prompt=prompt)
        # API providers (OpenRouter, …) work entirely over HTTP and never write
        # to the local filesystem, so there's nothing to snapshot.
        _track_files = not is_api_provider(provider_name)
        if _track_files:
            runtime.last_file_state = await self.scan_dir(work_dir)
        _, _, prev_total_in, prev_total_out = runtime.parser.get_token_usage()
        runtime.parser.clear_full_buffer()

        stream_queue: asyncio.Queue[str] = asyncio.Queue()
        task_done = asyncio.Event()
        interaction_event = asyncio.Event()
        interaction_event.set()  # Not waiting by default
        returncode_holder = [0]
        loop = asyncio.get_running_loop()

        def on_stream_line(line: str):
            actionable = runtime.parser.get_actionable_line(line)
            if actionable:
                # Detect interaction requests from the parser
                # actionable might look like "❓ Title" or "✅ Title"
                # but better check the parser's event category directly
                try:
                    loop.call_soon_threadsafe(stream_queue.put_nowait, actionable)
                except Exception:
                    pass
                
                # Check for interaction
                if interaction_callback and ("❓" in actionable or "✅" in actionable):
                    # We found a question or approval. Parser state should be updated.
                    from core.parser import ActionCategory
                    recent = runtime.parser.state.events[-1] if runtime.parser.state.events else None
                    if recent and recent.category in (ActionCategory.QUESTION, ActionCategory.APPROVAL):
                        # Trigger interaction in a background task so we don't block the stream reader
                        asyncio.create_task(handle_interaction(recent.category.value, recent.text))

                if stream_event_callback:
                    try:
                        stream_event_callback(actionable)
                    except Exception:
                        pass

        async def handle_interaction(kind: str, text: str):
            interaction_event.clear()
            try:
                response = await interaction_callback(kind, text)
                if response is not None:
                    await runtime.manager.write_stdin(response)
            finally:
                interaction_event.set()

        runtime.manager.set_stream_callback(on_stream_line)
        runtime.manager.set_final_result_callback(lambda text: runtime.parser.set_final_result(text))

        async def update_status_loop():
            while not task_done.is_set():
                await asyncio.sleep(1.5)
                # Wait if interaction is pending
                await interaction_event.wait()
                
                drained = False
                while not stream_queue.empty():
                    try:
                        stream_queue.get_nowait()
                        drained = True
                    except asyncio.QueueEmpty:
                        break
                if not drained and not runtime.parser.state.is_busy:
                    continue
                if not status_callback:
                    continue
                try:
                    progress = runtime.parser.get_progress_summary()
                    if status_prefix:
                        text = f"{status_prefix}\n\n{progress}" if progress else status_prefix
                    else:
                        text = status_formatter(progress) if status_formatter else progress
                    await status_callback(text)
                except Exception:
                    pass

        if is_api_provider(provider_name):
            _configure_api_manager(runtime.manager, session, prompt)

        async def run_agent():
            started_at = monotonic()
            try:
                # We wrap send_command to allow it to be interrupted or waited on
                # but since it's a subprocess, we just wait for it to finish.
                # The interaction happens via write_stdin in handle_interaction.
                rc = await runtime.manager.send_command(prompt, cwd=work_dir)
                returncode_holder[0] = rc
            except Exception as exc:
                log.error("Execution error: %s", exc, exc_info=True)
                returncode_holder[0] = -1
                session.last_task_result.error_text = str(exc)
                runtime.manager.mark_failure(str(exc))
            finally:
                session.last_task_result.duration_ms = int((monotonic() - started_at) * 1000)
                task_done.set()
                interaction_event.set()  # Unblock anything waiting

        asyncio.create_task(run_agent())
        # The status loop polls parser state and edits the status message.
        # When a stream renderer is active it handles all edits itself, so
        # running the loop in parallel would cause conflicting edits.
        status_task = (
            None if stream_event_callback is not None
            else asyncio.create_task(update_status_loop())
        )
        await task_done.wait()
        await asyncio.sleep(0.5)
        runtime.manager.set_stream_callback(None)
        runtime.manager.set_final_result_callback(None)
        if status_task is not None:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass

        returncode = returncode_holder[0]
        if _track_files and returncode == 0:
            current = await self.scan_dir(work_dir)
            new_files = [
                path for path in current.keys()
                if path not in runtime.last_file_state
            ]
            changed_files = [
                path for path in current.keys()
                if path in runtime.last_file_state
                and current[path] > runtime.last_file_state[path]
            ]
        else:
            new_files = []
            changed_files = []
        answer_text = runtime.parser.get_full_response() if returncode == 0 else ""
        last_in, last_out, total_in, total_out = runtime.parser.get_token_usage()
        run_total_in = max(0, total_in - prev_total_in)
        run_total_out = max(0, total_out - prev_total_out)
        task_result = TaskResult(
            provider=provider_name,
            model_name=getattr(runtime.manager, "model_name", "") or "",
            transport=provider_transport(provider_name),
            prompt=prompt,
            answer_text=answer_text or "",
            new_files=new_files,
            changed_files=changed_files,
            input_tokens=last_in,
            output_tokens=last_out,
            total_input_tokens=run_total_in,
            total_output_tokens=run_total_out,
            exit_code=returncode,
            started_at=session.last_task_result.started_at,
            duration_ms=session.last_task_result.duration_ms,
            finished_at=utc_now_iso(),
            error_text=session.last_task_result.error_text,
        )
        runtime.parser.clear_full_buffer()
        return task_result
