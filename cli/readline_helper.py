"""
Readline integration for the Forge shell.

Provides:
  - Persistent input history  (~/.forge_history)
  - Tab-completion for slash commands
  - History-search on arrow keys
  - Multiline input  (type  '''  or  \"\"\"  to start a block)

Gracefully no-ops on Windows or environments without readline.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HISTORY_FILE = Path.home() / ".forge_history"
_HISTORY_MAX = 5_000

# Whether readline is actually available
_readline_ok = False


# ── public API ────────────────────────────────────────────────────────────────

def setup(command_names: list[str]) -> None:
    """Call once at shell startup."""
    global _readline_ok
    try:
        import readline as _rl
    except ImportError:
        return

    _readline_ok = True

    # Persistent history
    _rl.set_history_length(_HISTORY_MAX)
    try:
        _rl.read_history_file(str(_HISTORY_FILE))
    except FileNotFoundError:
        pass

    import atexit
    atexit.register(_save_history)

    # Tab completion on /commands
    completer = _SlashCompleter(command_names)
    _rl.set_completer(completer.complete)
    _rl.set_completer_delims(" \t\n")

    if sys.platform == "darwin":
        # macOS ships libedit under the readline name
        _rl.parse_and_bind("bind ^I rl_complete")
    else:
        _rl.parse_and_bind("tab: complete")

    # History search — type a prefix, then ↑/↓
    _rl.parse_and_bind(r'"\e[A": history-search-backward')
    _rl.parse_and_bind(r'"\e[B": history-search-forward')


def read_input(display_prompt: str) -> str:
    """
    Read one line (or a multiline block) from the user.

    Multiline mode is entered by starting the input with ''' or \"\"\".
    The block ends when the user types ''' or \"\"\" on a line by itself.

    Raises EOFError / KeyboardInterrupt as usual.
    """
    line = input(display_prompt)

    # Detect multiline trigger
    stripped = line.strip()
    if stripped in ('"""', "'''"):
        return _read_multiline(stripped)

    return line


def add_to_history(text: str) -> None:
    """Manually add an entry (e.g. a composed multiline block) to readline history."""
    if not _readline_ok or not text.strip():
        return
    try:
        import readline as _rl
        _rl.add_history(text.replace("\n", " ↵ "))
    except Exception:
        pass


# ── internals ─────────────────────────────────────────────────────────────────

def _save_history() -> None:
    try:
        import readline as _rl
        _rl.write_history_file(str(_HISTORY_FILE))
    except Exception:
        pass


def _read_multiline(delimiter: str) -> str:
    """Collect lines until the user types the delimiter alone."""
    print(f"  \033[2m(multiline — end with {delimiter} on its own line)\033[0m")
    lines: list[str] = []
    while True:
        try:
            chunk = input("  … ")
        except (EOFError, KeyboardInterrupt):
            raise
        if chunk.strip() == delimiter:
            break
        lines.append(chunk)
    return "\n".join(lines)


class _SlashCompleter:
    """Tab-complete slash commands."""

    def __init__(self, commands: list[str]) -> None:
        self._commands = commands
        self._matches: list[str] = []

    def complete(self, text: str, state: int) -> str | None:
        if state == 0:
            if text.startswith("/"):
                self._matches = [c for c in self._commands if c.startswith(text)]
            else:
                self._matches = []
        try:
            return self._matches[state]
        except IndexError:
            return None
