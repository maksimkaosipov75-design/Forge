from dataclasses import dataclass, field
from typing import Literal


ProviderTransport = Literal["cli", "api"]


@dataclass(frozen=True)
class ModelDefinition:
    name: str
    label: str
    capabilities: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    label: str
    accent_color: str
    specialties: tuple[str, ...]
    transport: ProviderTransport = "cli"
    cli_env_var: str = ""
    default_cli_path: str = ""
    capabilities: tuple[str, ...] = ()
    default_model: str = ""
    available_models: tuple[ModelDefinition, ...] = field(default_factory=tuple)


SUPPORTED_PROVIDERS: dict[str, ProviderDefinition] = {
    "qwen": ProviderDefinition(
        name="qwen",
        label="Qwen",
        accent_color="violet",
        transport="cli",
        cli_env_var="QWEN_CLI_PATH",
        default_cli_path="qwen",
        specialties=("python", "data", "scripting", "general"),
        capabilities=("streaming", "session_resume", "file_editing", "shell_execution", "tool_use"),
        default_model="qwen3-coder-plus",
    ),
    "codex": ProviderDefinition(
        name="codex",
        label="Codex",
        accent_color="sky",
        transport="cli",
        cli_env_var="CODEX_CLI_PATH",
        default_cli_path="codex",
        specialties=("rust", "backend", "systems", "refactor"),
        capabilities=("streaming", "session_resume", "file_editing", "shell_execution", "tool_use"),
        default_model="gpt-5.3-codex",
    ),
    "claude": ProviderDefinition(
        name="claude",
        label="Claude",
        accent_color="orange",
        transport="cli",
        cli_env_var="CLAUDE_CLI_PATH",
        default_cli_path="claude",
        specialties=("ui", "ux", "gtk", "css", "writing"),
        capabilities=("streaming", "session_resume", "file_editing", "shell_execution", "tool_use"),
        default_model="claude-sonnet-4-6",
    ),
    "openrouter": ProviderDefinition(
        name="openrouter",
        label="OpenRouter",
        accent_color="green",
        transport="api",
        specialties=("planning", "review", "synthesis", "fallback"),
        capabilities=("streaming", "structured_output", "long_context", "low_cost", "planner", "reviewer", "synthesis"),
        default_model="qwen/qwen3-coder:free",
        available_models=(
            ModelDefinition(
                name="qwen/qwen3-coder:free",
                label="Qwen3 Coder Free",
                capabilities=("planner", "reviewer", "synthesis", "low_cost"),
                description="Free coding-oriented model for planning, analysis, and lightweight code tasks.",
            ),
            ModelDefinition(
                name="minimax/minimax-m2.5:free",
                label="MiniMax M2.5 Free",
                capabilities=("planner", "reviewer", "synthesis", "long_context", "low_cost"),
                description="Free general-purpose model suited to synthesis, review, and mixed prompts.",
            ),
            ModelDefinition(
                name="openrouter/free",
                label="OpenRouter Free Router",
                capabilities=("low_cost",),
                description="Best-effort free fallback route. Useful for experiments, not stable production runs.",
            ),
        ),
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


def provider_transport(value: str | None) -> ProviderTransport:
    return get_provider_definition(value).transport


def is_api_provider(value: str | None) -> bool:
    return provider_transport(value) == "api"


def is_cli_provider(value: str | None) -> bool:
    return provider_transport(value) == "cli"


def provider_default_model(value: str | None) -> str:
    return get_provider_definition(value).default_model


def list_provider_models(value: str | None) -> list[ModelDefinition]:
    return list(get_provider_definition(value).available_models)


def list_supported_provider_names() -> list[str]:
    return list(SUPPORTED_PROVIDERS.keys())


def list_supported_provider_labels() -> list[str]:
    return [item.label for item in SUPPORTED_PROVIDERS.values()]


def supported_provider_commands_text() -> str:
    return "|".join(list_supported_provider_names())
