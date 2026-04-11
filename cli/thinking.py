from __future__ import annotations

import re


def extract_thinking_chunk(line: str) -> str:
    """Return the raw thinking payload without trimming meaningful spaces."""
    if line.startswith("🧠 "):
        return line[2:]
    return line


def append_thinking_chunk(buffer: str, line: str) -> str:
    """Append a streaming thinking chunk to the existing buffer."""
    return buffer + extract_thinking_chunk(line)


def _md_to_rich_inline(text: str) -> str:
    """Convert inline markdown (**bold**, *italic*) to Rich markup."""
    # Escape Rich markup brackets first
    text = text.replace("[", r"\[")
    # **bold**
    text = re.sub(r"\*\*(.+?)\*\*", r"[bold]\1[/bold]", text)
    # *italic* (single star, not followed by another star)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"[italic]\1[/italic]", text)
    return text


def render_thinking_text(text: str, mode: str = "compact", *, rich: bool = False) -> str | None:
    """Render a thinking buffer for shell or TUI output."""
    if mode == "off":
        return None

    if mode == "compact":
        # Single-line preview: collapse whitespace, truncate to 180 chars
        normalized = text.replace("\r", "").replace("\n", " ")
        # Collapse multiple spaces
        normalized = re.sub(r" {2,}", " ", normalized).strip()
        if not normalized:
            normalized = "thinking…"
        if len(normalized) > 180:
            normalized = normalized[:179] + "…"
        if not rich:
            return f"  Thinking: {normalized}"
        escaped = _md_to_rich_inline(normalized)
        return f"  [#6fa86f]Thinking:[/#6fa86f] [dim]{escaped}[/dim]"

    # ── full mode ──────────────────────────────────────────────────────────────
    # Keep line breaks; render each line with inline markdown conversion.
    lines = text.replace("\r", "").split("\n")
    # Drop leading/trailing blank lines, keep up to 60 inner lines to avoid
    # flooding the display during a very long reasoning stream.
    stripped = [l for l in lines if l.strip()]
    preview_lines = stripped[:60]
    truncated = len(stripped) > 60

    if not preview_lines:
        preview_lines = ["thinking…"]

    if not rich:
        body = "\n    ".join(preview_lines)
        suffix = f"\n    … (+{len(stripped) - 60} more lines)" if truncated else ""
        return f"  Thinking:\n    {body}{suffix}"

    body_parts = ["    " + _md_to_rich_inline(l) for l in preview_lines]
    suffix = f"\n    [dim]… (+{len(stripped) - 60} more lines)[/dim]" if truncated else ""
    body = "\n".join(body_parts)
    return f"  [#6fa86f]Thinking:[/#6fa86f]\n{body}{suffix}"
