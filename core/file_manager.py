import html
import os
import json
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

log = logging.getLogger(__name__)


def _esc(text) -> str:
    """Escape HTML special characters in dynamic content."""
    return html.escape(str(text))


@dataclass
class Project:
    name: str
    path: str
    created_at: str = ""


class FileManager:
    """Filesystem manager, provider-agnostic."""

    def __init__(self, projects_file: str = "projects.json"):
        self.projects_file = projects_file
        self.projects: dict[str, Project] = {}
        self.current_project: Optional[Project] = None
        self.working_dir = Path(os.getcwd()).resolve()
        self._load_projects()

    def _load_projects(self):
        if os.path.exists(self.projects_file):
            try:
                with open(self.projects_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for name, proj in data.items():
                    self.projects[name] = Project(**proj)
            except Exception as e:
                log.warning(f"Failed to load projects: {e}")

    def _save_projects(self):
        data = {name: asdict(proj) for name, proj in self.projects.items()}
        with open(self.projects_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def set_working_dir(self, path: str) -> str:
        """Set the working directory."""
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"❌ Path does not exist: {_esc(p)}"
        if not p.is_dir():
            return f"❌ Not a directory: {_esc(p)}"
        self.working_dir = p
        return f"📂 Working directory: <code>{_esc(p)}</code>"

    def get_working_dir(self) -> Path:
        return self.working_dir

    def list_dir(self, path: str = None) -> str:
        """Directory listing."""
        target = Path(path).expanduser().resolve() if path else self.working_dir
        if path:
            if err := self._check_path_safe(target):
                return err
        if not target.exists():
            return f"❌ Path does not exist: {_esc(target)}"
        if not target.is_dir():
            return f"❌ Not a directory: {_esc(target)}"

        items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = [f"📁 <code>{_esc(target)}</code>\n"]

        for item in items:
            icon = "📂" if item.is_dir() else "📄"
            size = ""
            if item.is_file():
                try:
                    size = f" ({self._format_size(item.stat().st_size)})"
                except OSError:
                    pass
            lines.append(f"{icon} <code>{_esc(item.name)}</code>{size}")

        return "\n".join(lines)

    def _check_path_safe(self, target: Path) -> Optional[str]:
        """Validates path is within the working directory."""
        try:
            target.resolve().relative_to(self.working_dir.resolve())
            return None
        except ValueError:
            return f"❌ Access denied: path is outside the working directory"

    def read_file(self, path: str, max_lines: int = 100) -> str:
        """Read a file."""
        target = Path(path)
        if not target.is_absolute():
            target = self.working_dir / target
        target = target.resolve()

        if err := self._check_path_safe(target):
            return err
        if not target.exists():
            return f"❌ File not found: <code>{_esc(target.name)}</code>"
        if target.is_dir():
            return f"❌ Is a directory: <code>{_esc(target.name)}</code>"
        if target.stat().st_size > 100_000:
            return f"❌ File too large: <code>{_esc(target.name)}</code> ({self._format_size(target.stat().st_size)})"

        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            shown = lines[:max_lines]
            text = "".join(shown)
            suffix = f"\n\n... ({len(lines) - max_lines} more lines)" if len(lines) > max_lines else ""
            return f"📄 <code>{_esc(target.name)}</code>:\n<pre>{_esc(text)}</pre>{suffix}"
        except PermissionError:
            return f"❌ No read permission: <code>{_esc(target.name)}</code>"
        except Exception as e:
            return f"❌ Read error: {_esc(e)}"

    def tree(self, path: str = None, max_depth: int = 3, current_depth: int = 0) -> str:
        """File tree."""
        target = Path(path).expanduser().resolve() if path else self.working_dir
        if not target.exists() or not target.is_dir():
            return f"❌ Directory not found: {_esc(target)}"

        lines = [f"{target}"]
        self._build_tree(target, lines, max_depth, current_depth, prefix="")
        return "\n".join(lines)

    def _build_tree(self, directory: Path, lines: list, max_depth: int, current_depth: int, prefix: str):
        if current_depth >= max_depth:
            return

        items = sorted(directory.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        # Ограничиваем количество элементов
        items = items[:50]

        for i, item in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            icon = "📂" if item.is_dir() else "📄"
            lines.append(f"{prefix}{connector}{icon} {item.name}")

            if item.is_dir():
                extension = "    " if is_last else "│   "
                self._build_tree(item, lines, max_depth, current_depth + 1, prefix + extension)

    def set_project(self, name: str, path: str) -> str:
        """Save a project."""
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"❌ Path does not exist: {_esc(p)}"
        self.projects[name] = Project(name=name, path=str(p))
        self.current_project = self.projects[name]
        self.working_dir = p
        self._save_projects()
        return f"✅ Project <code>{_esc(name)}</code> saved: <code>{_esc(p)}</code>"

    def load_project(self, name: str) -> str:
        """Load a project."""
        if name not in self.projects:
            available = ", ".join(_esc(k) for k in self.projects.keys()) or "none"
            return f"❌ Project <code>{_esc(name)}</code> not found. Available: {available}"
        proj = self.projects[name]
        if not Path(proj.path).exists():
            return f"❌ Project path does not exist: <code>{_esc(proj.path)}</code>"
        self.current_project = proj
        self.working_dir = Path(proj.path)
        return f"📂 Loaded project <code>{_esc(name)}</code>: <code>{_esc(proj.path)}</code>"

    def list_projects(self) -> str:
        """List projects."""
        if not self.projects:
            return "📋 No saved projects."
        lines = ["📋 Projects:\n"]
        for name, proj in self.projects.items():
            current = " ← <b>current</b>" if self.current_project and self.current_project.name == name else ""
            lines.append(f"• <code>{_esc(name)}</code> → <code>{_esc(proj.path)}</code>{current}")
        return "\n".join(lines)

    def get_project_context(self) -> str:
        """Current project context."""
        if not self.current_project:
            return f"📂 Working directory: <code>{_esc(self.working_dir)}</code>\nNo project selected."

        proj = self.current_project
        # Считаем файлы
        try:
            files = list(self.working_dir.rglob("*"))
            total_files = len([f for f in files if f.is_file()])
            total_dirs = len([f for f in files if f.is_dir()])
        except PermissionError:
            total_files = total_dirs = "?"

        return (
            f"📂 Project: <code>{_esc(proj.name)}</code>\n"
            f"📍 Path: <code>{_esc(proj.path)}</code>\n"
            f"📄 Files: {total_files} | 📁 Dirs: {total_dirs}"
        )

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.0f}{unit}"
            size /= 1024
        return f"{size:.0f}TB"
