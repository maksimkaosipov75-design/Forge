from dataclasses import dataclass


@dataclass(frozen=True)
class PromptValidationResult:
    allowed: bool
    reason: str = ""


SUSPICIOUS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ignore previous instructions", "найдена попытка отключить системные инструкции"),
    ("disregard all prior instructions", "найдена попытка отключить системные инструкции"),
    ("reveal system prompt", "запрос на раскрытие системного промпта"),
    ("show hidden prompt", "запрос на раскрытие скрытого промпта"),
    ("print your system instructions", "запрос на раскрытие системных инструкций"),
    ("exfiltrate", "обнаружен запрос на эксфильтрацию данных"),
    ("steal secrets", "обнаружен запрос на кражу секретов"),
    ("read /etc/shadow", "обнаружен опасный запрос на чтение системных секретов"),
)


def validate_prompt(prompt: str, max_length: int = 12000) -> PromptValidationResult:
    text = (prompt or "").strip()
    if not text:
        return PromptValidationResult(False, "пустой запрос")

    if len(text) > max_length:
        return PromptValidationResult(False, f"запрос слишком длинный: {len(text)} символов")

    lowered = text.lower()
    for pattern, reason in SUSPICIOUS_PATTERNS:
        if pattern in lowered:
            return PromptValidationResult(False, reason)

    return PromptValidationResult(True, "")
