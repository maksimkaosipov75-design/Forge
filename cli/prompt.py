"""
Interactive prompt primitives — masked input, confirm, free text.

Designed to work with or without Rich, fall back gracefully on
non-TTY environments (pipes, CI).  No external dependencies beyond
stdlib + whatever is already installed (rich, textual).
"""

import os
import sys

# ── low-level masked input ────────────────────────────────────────────────────

def _read_masked_unix(display_prompt: str) -> str:
    """Read a secret string on a POSIX TTY, echoing '*' per character."""
    import tty
    import termios

    sys.stdout.write(display_prompt)
    sys.stdout.flush()

    chars: list[str] = []
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                break
            elif ch == "\x03":          # Ctrl-C
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise KeyboardInterrupt
            elif ch == "\x04":          # Ctrl-D / EOF
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise EOFError
            elif ch in ("\x7f", "\x08"):  # Backspace / Delete
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ord(ch) >= 32:         # printable
                chars.append(ch)
                sys.stdout.write("*")
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return "".join(chars)


def read_masked(display_prompt: str = "Secret: ") -> str:
    """
    Read a secret string from the terminal.

    Shows '*' for each character typed.  Falls back to getpass on
    Windows or when stdin is not a TTY.
    """
    if not sys.stdin.isatty() or os.name == "nt":
        import getpass
        return getpass.getpass(display_prompt)

    try:
        return _read_masked_unix(display_prompt)
    except (ImportError, Exception):
        import getpass
        return getpass.getpass(display_prompt)


def read_confirm(display_prompt: str = "Continue? [y/N]: ", default: bool = False) -> bool:
    """
    Prompt for a yes/no answer.  Returns True for 'y'/'yes', False otherwise.
    Default is used when the user just presses Enter.
    """
    while True:
        try:
            raw = input(display_prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        sys.stdout.write("  Please enter y or n.\n")
        sys.stdout.flush()


def read_text(display_prompt: str = "Answer: ") -> str | None:
    """
    Read a plain text answer.  Returns None on Ctrl-C / EOF / empty Enter.
    """
    try:
        value = input(display_prompt).strip()
        return value if value else None
    except (EOFError, KeyboardInterrupt):
        return None
