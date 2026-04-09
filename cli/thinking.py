from __future__ import annotations


def extract_thinking_chunk(line: str) -> str:
    """Return the raw thinking payload without trimming meaningful spaces."""
    if line.startswith("🧠 "):
        return line[2:]
    return line


def append_thinking_chunk(buffer: str, line: str) -> str:
    """Append a streaming thinking chunk to the existing buffer."""
    return buffer + extract_thinking_chunk(line)


def render_thinking_text(text: str, mode: str = "compact", *, rich: bool = False) -> str | None:
    """Render a thinking buffer for shell or TUI output."""
    if mode == "off":
        return None

    normalized = text.replace("\r", "").replace("\n", " ")
    if not normalized:
        normalized = "thinking"

    if mode == "compact" and len(normalized) > 180:
        normalized = normalized[:179] + "…"

    if not rich:
        return f"  Thinking: {normalized}"

    escaped = normalized.replace("[", chr(92) + "[")
    return f"  [#6fa86f]Thinking:[/#6fa86f] [dim]{escaped}[/dim]"
