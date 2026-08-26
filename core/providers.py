from dataclasses import dataclass, field
from typing import Literal


ProviderTransport = Literal["cli", "api"]


@dataclass(frozen=True)
class ModelDefinition:
    name: str
    label: str
    capabilities: tuple[str, ...] = ()
    description: str = ""
    aliases: tuple[str, ...] = ()


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
        available_models=(
            ModelDefinition(
                name="qwen3-coder-plus",
                label="Qwen3 Coder Plus",
                capabilities=("coding", "tool_use", "long_context"),
                description="Default coding model for Qwen CLI workflows.",
                aliases=("qwen coder plus", "coder plus", "qwen3 coder"),
            ),
            ModelDefinition(
                name="qwen3-coder-flash",
                label="Qwen3 Coder Flash",
                capabilities=("coding", "fast"),
                description="Faster coding-oriented Qwen model for smaller edits and quick questions.",
                aliases=("qwen flash", "coder flash", "flash"),
            ),
            ModelDefinition(
                name="qwen3-max",
                label="Qwen3 Max",
                capabilities=("general", "reasoning", "long_context"),
                description="General high-capability Qwen model when the CLI account exposes it.",
                aliases=("qwen max", "max"),
            ),
            ModelDefinition(
                name="qwen-plus",
                label="Qwen Plus",
                capabilities=("general", "balanced"),
                description="Balanced Qwen model for everyday implementation and analysis.",
                aliases=("plus", "qwen plus"),
            ),
            ModelDefinition(
                name="qwen-turbo",
                label="Qwen Turbo",
                capabilities=("fast", "general"),
                description="Lower-latency Qwen model for lightweight tasks.",
                aliases=("turbo", "qwen turbo"),
            ),
            ModelDefinition(
                name="qwen-long",
                label="Qwen Long",
                capabilities=("long_context", "general"),
                description="Long-context Qwen model where available in the configured account.",
                aliases=("long", "qwen long"),
            ),
        ),
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
        available_models=(
            ModelDefinition(
                name="gpt-5.3-codex",
                label="GPT-5.3 Codex",
                capabilities=("coding", "agentic", "tool_use"),
                description="Default Codex coding model for repository edits and reviews.",
                aliases=("codex", "5.3 codex", "gpt codex"),
            ),
            ModelDefinition(
                name="gpt-5.5",
                label="GPT-5.5",
                capabilities=("reasoning", "coding", "analysis"),
                description="Frontier general model when your Codex CLI account exposes it.",
                aliases=("5.5", "gpt 5.5"),
            ),
            ModelDefinition(
                name="gpt-5.4",
                label="GPT-5.4",
                capabilities=("coding", "analysis"),
                description="Strong general coding model for everyday work.",
                aliases=("5.4", "gpt 5.4"),
            ),
            ModelDefinition(
                name="gpt-5.4-mini",
                label="GPT-5.4 Mini",
                capabilities=("fast", "coding"),
                description="Fast, lower-cost Codex-compatible model for smaller tasks.",
                aliases=("mini", "5.4 mini"),
            ),
            ModelDefinition(
                name="gpt-5.2",
                label="GPT-5.2",
                capabilities=("coding", "analysis"),
                description="Compatibility option for environments pinned to GPT-5.2.",
                aliases=("5.2", "gpt 5.2"),
            ),
        ),
    ),
    "claude": ProviderDefinition(
        name="claude",
        label="Claude",
        accent_color="orange",
        transport="cli",
        cli_env_var="CLAUDE_CLI_PATH",
        default_cli_path="claude",
        specialties=("ui", "ux", "gtk", "css", "writing"),
        capabilities=("streaming", "session_resume", "file_editing", "shell_execution", "tool_use", "thinking"),
        default_model="claude-sonnet-4-6",
        available_models=(
            ModelDefinition(
                name="claude-sonnet-4-6",
                label="Claude Sonnet 4.6",
                capabilities=("coding", "ui", "thinking", "tool_use"),
                description="Default Claude Code model; strong for UI, refactors, and product polish.",
                aliases=("sonnet", "claude sonnet", "sonnet 4.6"),
            ),
            ModelDefinition(
                name="opus",
                label="Opus alias",
                capabilities=("reasoning", "thinking", "tool_use"),
                description="Claude CLI alias for the latest Opus available to the account.",
                aliases=("latest opus",),
            ),
            ModelDefinition(
                name="claude-opus-4-5",
                label="Claude Opus 4.5",
                capabilities=("reasoning", "thinking", "tool_use"),
                description="High-reasoning Claude model where available in the account.",
                aliases=("opus 4.5", "claude opus"),
            ),
            ModelDefinition(
                name="claude-haiku-4-5",
                label="Claude Haiku 4.5",
                capabilities=("fast", "tool_use"),
                description="Fast Claude option for quick questions and lightweight edits.",
                aliases=("haiku", "claude haiku"),
            ),
        ),
    ),
    "openrouter": ProviderDefinition(
        name="openrouter",
        label="OpenRouter",
        accent_color="green",
        transport="api",
        specialties=("planning", "review", "synthesis", "fallback"),
        capabilities=("streaming", "structured_output", "long_context", "low_cost", "planner", "reviewer", "synthesis", "thinking"),
        default_model="qwen/qwen3-coder:free",
        available_models=(
            ModelDefinition(
                name="qwen/qwen3-coder:free",
                label="Qwen3 Coder Free",
                capabilities=("planner", "reviewer", "synthesis", "low_cost", "thinking"),
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
    "local": ProviderDefinition(
        name="local",
        label="Local LLM",
        accent_color="red",
        transport="api",
        specialties=("local", "private", "coding", "chat", "offline"),
        capabilities=("streaming", "file_editing", "shell_execution", "tool_use", "web_fetch", "planner", "reviewer", "synthesis"),
        default_model="qwen2.5-coder:7b",
        available_models=(
            ModelDefinition(
                name="qwen2.5-coder:7b",
                label="Qwen2.5 Coder 7B",
                capabilities=("coding", "tool_use", "local"),
                description="Good default for Ollama or other local OpenAI-compatible coding servers.",
                aliases=("qwen coder", "qwen local", "coder 7b", "qwen2.5 coder"),
            ),
            ModelDefinition(
                name="qwen2.5-coder:14b",
                label="Qwen2.5 Coder 14B",
                capabilities=("coding", "tool_use", "local"),
                description="Stronger local coding option when your machine can run it.",
                aliases=("coder 14b", "qwen 14b"),
            ),
            ModelDefinition(
                name="devstral:latest",
                label="Devstral",
                capabilities=("coding", "agentic", "local"),
                description="Local coding-agent model commonly served by Ollama.",
                aliases=("devstral",),
            ),
            ModelDefinition(
                name="llama3.1:8b",
                label="Llama 3.1 8B",
                capabilities=("chat", "local"),
                description="General local chat fallback.",
                aliases=("llama", "llama 8b"),
            ),
            ModelDefinition(
                name="mistral:latest",
                label="Mistral",
                capabilities=("chat", "fast", "local"),
                description="Fast local chat option.",
                aliases=("mistral",),
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


def resolve_provider_model_definition(value: str | None, model_name: str | None) -> ModelDefinition | None:
    cleaned = (model_name or "").strip().casefold()
    if not cleaned:
        cleaned = provider_default_model(value).casefold()
    for model in list_provider_models(value):
        names = (model.name, model.label, *model.aliases)
        if any(cleaned == item.casefold() for item in names if item):
            return model
    return None


def provider_supports_thinking(value: str | None, model_name: str | None = "") -> bool:
    definition = get_provider_definition(value)
    model = resolve_provider_model_definition(value, model_name)
    if model is not None:
        return "thinking" in model.capabilities or "thinking" in definition.capabilities
    return "thinking" in definition.capabilities


def list_supported_provider_names() -> list[str]:
    return list(SUPPORTED_PROVIDERS.keys())


def list_supported_provider_labels() -> list[str]:
    return [item.label for item in SUPPORTED_PROVIDERS.values()]


def supported_provider_commands_text() -> str:
    return "|".join(list_supported_provider_names())
