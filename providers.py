from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    label: str
    accent_color: str
    cli_env_var: str
    default_cli_path: str
    specialties: tuple[str, ...]


SUPPORTED_PROVIDERS: dict[str, ProviderDefinition] = {
    "qwen": ProviderDefinition(
        name="qwen",
        label="Qwen",
        accent_color="violet",
        cli_env_var="QWEN_CLI_PATH",
        default_cli_path="qwen",
        specialties=("python", "data", "scripting", "general"),
    ),
    "codex": ProviderDefinition(
        name="codex",
        label="Codex",
        accent_color="sky",
        cli_env_var="CODEX_CLI_PATH",
        default_cli_path="codex",
        specialties=("rust", "backend", "systems", "refactor"),
    ),
    "claude": ProviderDefinition(
        name="claude",
        label="Claude",
        accent_color="orange",
        cli_env_var="CLAUDE_CLI_PATH",
        default_cli_path="claude",
        specialties=("ui", "ux", "gtk", "css", "writing"),
    ),
}


def normalize_provider_name(value: str | None, default: str = "qwen") -> str:
    normalized = (value or default).strip().lower()
    if normalized in SUPPORTED_PROVIDERS:
        return normalized
    return default


def is_supported_provider(value: str | None) -> bool:
    return (value or "").strip().lower() in SUPPORTED_PROVIDERS


def get_provider_definition(value: str | None) -> ProviderDefinition:
    return SUPPORTED_PROVIDERS[normalize_provider_name(value)]


def list_supported_provider_names() -> list[str]:
    return list(SUPPORTED_PROVIDERS.keys())


def list_supported_provider_labels() -> list[str]:
    return [item.label for item in SUPPORTED_PROVIDERS.values()]


def supported_provider_commands_text() -> str:
    return "|".join(list_supported_provider_names())
