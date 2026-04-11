from dataclasses import dataclass


@dataclass(frozen=True)
class PromptValidationResult:
    allowed: bool
    reason: str = ""


SUSPICIOUS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ignore previous instructions", "attempt to override system instructions"),
    ("disregard all prior instructions", "attempt to override system instructions"),
    ("reveal system prompt", "request to reveal system prompt"),
    ("show hidden prompt", "request to reveal hidden prompt"),
    ("print your system instructions", "request to reveal system instructions"),
    ("exfiltrate", "data exfiltration attempt detected"),
    ("steal secrets", "secret theft attempt detected"),
    ("read /etc/shadow", "dangerous system secrets read attempt"),
)


def validate_prompt(prompt: str, max_length: int = 12000) -> PromptValidationResult:
    text = (prompt or "").strip()
    if not text:
        return PromptValidationResult(False, "empty prompt")

    if len(text) > max_length:
        return PromptValidationResult(False, f"prompt too long: {len(text)} characters")

    lowered = text.lower()
    for pattern, reason in SUSPICIOUS_PATTERNS:
        if pattern in lowered:
            return PromptValidationResult(False, reason)

    return PromptValidationResult(True, "")
