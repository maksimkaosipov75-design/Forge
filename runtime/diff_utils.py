"""
File diff utilities for Forge.

Used by ToolExecutor (API mode) and the CLI file-change watcher to emit
``📊`` diff events that show added/deleted lines in the Telegram status message.
"""
from __future__ import annotations

import difflib

# Max diff-line pairs included in a single notify event (keeps events compact).
MAX_DIFF_LINES = 8


def compute_diff(old: str, new: str) -> tuple[int, int, list[tuple[str, str]]]:
    """
    Diff *old* against *new* line-by-line.

    Returns:
        (added_count, deleted_count, changes)
        where *changes* is a list of ``('+'/'-', line_text)`` pairs,
        capped at *MAX_DIFF_LINES* total entries (added first, then deleted).
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    added: list[str] = []
    deleted: list[str] = []

    for dl in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
        if dl.startswith('+') and not dl.startswith('+++'):
            added.append(dl[1:])
        elif dl.startswith('-') and not dl.startswith('---'):
            deleted.append(dl[1:])

    # Build change list: take added first, then deleted, cap total at MAX_DIFF_LINES
    half = MAX_DIFF_LINES // 2
    changes: list[tuple[str, str]] = (
        [('+', l) for l in added[:half]]
        + [('-', l) for l in deleted[: MAX_DIFF_LINES - min(half, len(added))]]
    )
    return len(added), len(deleted), changes


def format_diff_notify(rel_path: str, old: str, new: str) -> str:
    """
    Build a ``📊`` notify event string for a file change.

    Format (each ``\\n``-separated line is part of the *same* event)::

        📊 rel/path/file.py  +N -M
        + added line 1
        + added line 2
        - deleted line

    Returns an empty string when the content is identical.
    """
    added, deleted, changes = compute_diff(old, new)
    if added == 0 and deleted == 0:
        return ""

    parts = [f"📊 {rel_path}  +{added} -{deleted}"]
    for kind, line in changes:
        parts.append(f"{kind} {line[:160]}")
    return "\n".join(parts)


def parse_git_diff(diff_output: str) -> tuple[int, int, list[tuple[str, str]]]:
    """
    Parse raw ``git diff`` text into ``(added, deleted, changes)``.
    Used in CLI mode where we run git to get the diff of a model-written file.
    """
    added: list[str] = []
    deleted: list[str] = []

    for line in diff_output.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            added.append(line[1:])
        elif line.startswith('-') and not line.startswith('---'):
            deleted.append(line[1:])

    half = MAX_DIFF_LINES // 2
    changes: list[tuple[str, str]] = (
        [('+', l) for l in added[:half]]
        + [('-', l) for l in deleted[: MAX_DIFF_LINES - min(half, len(added))]]
    )
    return len(added), len(deleted), changes


def format_diff_notify_from_git(rel_path: str, diff_output: str) -> str:
    """Build a ``📊`` notify string from raw ``git diff`` output."""
    added, deleted, changes = parse_git_diff(diff_output)
    if added == 0 and deleted == 0:
        return ""

    parts = [f"📊 {rel_path}  +{added} -{deleted}"]
    for kind, line in changes:
        parts.append(f"{kind} {line[:160]}")
    return "\n".join(parts)
