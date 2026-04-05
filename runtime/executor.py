import asyncio
import logging
from pathlib import Path
from time import monotonic
from typing import Awaitable, Callable

from task_models import ProviderRuntime, TaskResult, utc_now_iso


log = logging.getLogger(__name__)


StatusCallback = Callable[[str], Awaitable[None]]
StatusFormatter = Callable[[str], str]


class ExecutionService:
    @staticmethod
    def _scan_dir_sync(directory: Path) -> dict[str, float]:
        result = {}
        try:
            for item in directory.rglob("*"):
                if item.is_file() and not any(part.startswith(".") for part in item.parts):
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
    ) -> TaskResult:
        work_dir = session.file_mgr.get_working_dir()
        session.last_task_result = TaskResult(provider=provider_name, prompt=prompt)
        runtime.last_file_state = await self.scan_dir(work_dir)
        runtime.parser.clear_full_buffer()

        stream_queue: asyncio.Queue[str] = asyncio.Queue()
        task_done = asyncio.Event()
        returncode_holder = [0]
        loop = asyncio.get_running_loop()

        def on_stream_line(line: str):
            actionable = runtime.parser.get_actionable_line(line)
            if actionable:
                try:
                    loop.call_soon_threadsafe(stream_queue.put_nowait, actionable)
                except Exception:
                    pass
                if stream_event_callback:
                    try:
                        stream_event_callback(actionable)
                    except Exception:
                        pass

        runtime.manager.set_stream_callback(on_stream_line)
        runtime.manager.set_final_result_callback(lambda text: runtime.parser.set_final_result(text))

        async def update_status_loop():
            while not task_done.is_set():
                await asyncio.sleep(1.5)
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

        async def run_agent():
            started_at = monotonic()
            try:
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

        asyncio.create_task(run_agent())
        status_task = asyncio.create_task(update_status_loop())
        await task_done.wait()
        await asyncio.sleep(0.5)
        runtime.manager.set_stream_callback(None)
        runtime.manager.set_final_result_callback(None)
        status_task.cancel()
        try:
            await status_task
        except asyncio.CancelledError:
            pass

        returncode = returncode_holder[0]
        current = await self.scan_dir(work_dir)
        new_files = [path for path in current.keys() if path not in runtime.last_file_state]
        changed_files = [
            path for path in current.keys()
            if path in runtime.last_file_state and current[path] > runtime.last_file_state[path]
        ]
        answer_text = runtime.parser.get_full_response() if returncode == 0 else ""
        task_result = TaskResult(
            provider=provider_name,
            prompt=prompt,
            answer_text=answer_text or "",
            new_files=new_files if returncode == 0 else [],
            changed_files=changed_files if returncode == 0 else [],
            exit_code=returncode,
            started_at=session.last_task_result.started_at,
            duration_ms=session.last_task_result.duration_ms,
            finished_at=utc_now_iso(),
            error_text=session.last_task_result.error_text,
        )
        runtime.parser.clear_full_buffer()
        return task_result
