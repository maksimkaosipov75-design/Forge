import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import settings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RemoteControlStatus:
    enabled: bool = False
    pid: int = 0
    started_at: str = ""
    log_path: str = ""
    state_file: str = ""

    @property
    def is_running(self) -> bool:
        if self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        return True


class RemoteControlManager:
    def __init__(self, state_root: Path | None = None):
        self.state_root = state_root or Path(".session_data")
        self.state_root.mkdir(exist_ok=True)
        self.state_file = self.state_root / "remote_control.json"
        self.log_file = self.state_root / "remote_control.log"
        self.repo_root = Path(__file__).resolve().parent.parent

    def load_status(self) -> RemoteControlStatus:
        if not self.state_file.exists():
            return RemoteControlStatus(state_file=str(self.state_file))
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return RemoteControlStatus(state_file=str(self.state_file))
        return RemoteControlStatus(
            enabled=bool(payload.get("enabled", False)),
            pid=int(payload.get("pid", 0) or 0),
            started_at=str(payload.get("started_at", "")),
            log_path=str(payload.get("log_path", self.log_file)),
            state_file=str(self.state_file),
        )

    def save_status(self, status: RemoteControlStatus):
        payload = {
            "enabled": status.enabled,
            "pid": status.pid,
            "started_at": status.started_at,
            "log_path": status.log_path or str(self.log_file),
        }
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def start(self) -> RemoteControlStatus:
        if not settings.TELEGRAM_TOKEN:
            raise RuntimeError("TELEGRAM_TOKEN is not configured.")

        current = self.load_status()
        if current.is_running:
            return current

        with self.log_file.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=self.repo_root,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        status = RemoteControlStatus(
            enabled=True,
            pid=process.pid,
            started_at=utc_now_iso(),
            log_path=str(self.log_file),
            state_file=str(self.state_file),
        )
        self.save_status(status)
        return status

    def stop(self) -> RemoteControlStatus:
        status = self.load_status()
        if status.pid > 0 and status.is_running:
            os.kill(status.pid, signal.SIGTERM)
        stopped = RemoteControlStatus(
            enabled=False,
            pid=0,
            started_at="",
            log_path=status.log_path or str(self.log_file),
            state_file=str(self.state_file),
        )
        self.save_status(stopped)
        return stopped

    def tail_logs(self, limit: int = 40) -> str:
        if not self.log_file.exists():
            return ""
        lines = self.log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-limit:])
