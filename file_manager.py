import os
import json
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

log = logging.getLogger(__name__)


@dataclass
class Project:
    name: str
    path: str
    created_at: str = ""


class FileManager:
    """Управление файловой системой независимо от qwen."""

    def __init__(self, projects_file: str = "projects.json"):
        self.projects_file = projects_file
        self.projects: dict[str, Project] = {}
        self.current_project: Optional[Project] = None
        self.working_dir = Path.cwd()
        self._load_projects()

    def _load_projects(self):
        if os.path.exists(self.projects_file):
            try:
                with open(self.projects_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for name, proj in data.items():
                    self.projects[name] = Project(**proj)
            except Exception as e:
                log.warning(f"Ошибка загрузки проектов: {e}")

    def _save_projects(self):
        data = {name: asdict(proj) for name, proj in self.projects.items()}
        with open(self.projects_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def set_working_dir(self, path: str) -> str:
        """Установить рабочую директорию."""
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"❌ Путь не существует: {p}"
        if not p.is_dir():
            return f"❌ Это не директория: {p}"
        self.working_dir = p
        return f"📂 Рабочая директория: `{p}`"

    def get_working_dir(self) -> Path:
        return self.working_dir

    def list_dir(self, path: str = None) -> str:
        """Содержимое директории."""
        target = Path(path).expanduser().resolve() if path else self.working_dir
        if not target.exists():
            return f"❌ Путь не существует: {target}"
        if not target.is_dir():
            return f"❌ Это не директория: {target}"

        items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = [f"📁 `{target}`\n"]

        for item in items:
            icon = "📂" if item.is_dir() else "📄"
            size = ""
            if item.is_file():
                try:
                    size = f" ({self._format_size(item.stat().st_size)})"
                except OSError:
                    pass
            lines.append(f"{icon} `{item.name}`{size}")

        return "\n".join(lines)

    def read_file(self, path: str, max_lines: int = 100) -> str:
        """Прочитать файл."""
        target = Path(path)
        if not target.is_absolute():
            target = self.working_dir / target
        target = target.resolve()

        if not target.exists():
            return f"❌ Файл не найден: `{target.name}`"
        if target.is_dir():
            return f"❌ Это директория: `{target.name}`"
        if target.stat().st_size > 100_000:
            return f"❌ Файл слишком большой: `{target.name}` ({self._format_size(target.stat().st_size)})"

        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            shown = lines[:max_lines]
            text = "".join(shown)
            suffix = f"\n\n... (ещё {len(lines) - max_lines} строк)" if len(lines) > max_lines else ""
            return f"📄 `{target.name}`:\n```\n{text}\n```{suffix}"
        except PermissionError:
            return f"❌ Нет прав на чтение: `{target.name}`"
        except Exception as e:
            return f"❌ Ошибка чтения: {e}"

    def tree(self, path: str = None, max_depth: int = 3, current_depth: int = 0) -> str:
        """Дерево файлов."""
        target = Path(path).expanduser().resolve() if path else self.working_dir
        if not target.exists() or not target.is_dir():
            return f"❌ Директория не найдена: {target}"

        lines = [f"📁 `{target}`"]
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
            lines.append(f"{prefix}{connector}{icon} `{item.name}`")

            if item.is_dir():
                extension = "    " if is_last else "│   "
                self._build_tree(item, lines, max_depth, current_depth + 1, prefix + extension)

    def set_project(self, name: str, path: str) -> str:
        """Сохранить проект."""
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return f"❌ Путь не существует: {p}"
        self.projects[name] = Project(name=name, path=str(p))
        self.current_project = self.projects[name]
        self.working_dir = p
        self._save_projects()
        return f"✅ Проект `{name}` сохранён: `{p}`"

    def load_project(self, name: str) -> str:
        """Загрузить проект."""
        if name not in self.projects:
            available = ", ".join(self.projects.keys()) or "нет"
            return f"❌ Проект `{name}` не найден. Доступные: {available}"
        proj = self.projects[name]
        if not Path(proj.path).exists():
            return f"❌ Путь проекта не существует: `{proj.path}`"
        self.current_project = proj
        self.working_dir = Path(proj.path)
        return f"📂 Загружен проект `{name}`: `{proj.path}`"

    def list_projects(self) -> str:
        """Список проектов."""
        if not self.projects:
            return "📋 Нет сохранённых проектов."
        lines = ["📋 Проекты:\n"]
        for name, proj in self.projects.items():
            current = " ← **текущий**" if self.current_project and self.current_project.name == name else ""
            lines.append(f"• `{name}` → `{proj.path}`{current}")
        return "\n".join(lines)

    def get_project_context(self) -> str:
        """Контекст текущего проекта."""
        if not self.current_project:
            return f"📂 Рабочая директория: `{self.working_dir}`\nПроект не выбран."

        proj = self.current_project
        # Считаем файлы
        try:
            files = list(self.working_dir.rglob("*"))
            total_files = len([f for f in files if f.is_file()])
            total_dirs = len([f for f in files if f.is_dir()])
        except PermissionError:
            total_files = total_dirs = "?"

        return (
            f"📂 Проект: `{proj.name}`\n"
            f"📍 Путь: `{proj.path}`\n"
            f"📄 Файлов: {total_files} | 📁 Директорий: {total_dirs}"
        )

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.0f}{unit}"
            size /= 1024
        return f"{size:.0f}TB"
