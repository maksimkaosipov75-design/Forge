import os
import logging
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional dependency in minimal environments
    def load_dotenv():
        return False

load_dotenv()
log = logging.getLogger(__name__)


def _get_int_env(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Invalid integer for %s=%r, using default %d", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning("%s=%d is below minimum %d, using minimum", name, value, minimum)
        return minimum
    return value


class Settings:
    def __init__(self):
        self.TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
        ids_str = os.getenv("ALLOWED_USER_IDS", "")
        self.ALLOWED_USER_IDS = [x.strip() for x in ids_str.split(",") if x.strip()]
        self.CLI_PROVIDER = os.getenv("CLI_PROVIDER", "qwen")
        self.CLI_PATH = os.getenv("CLI_PATH", "qwen")
        self.QWEN_CLI_PATH = os.getenv("QWEN_CLI_PATH", self.CLI_PATH)
        self.CODEX_CLI_PATH = os.getenv("CODEX_CLI_PATH", "codex")
        self.CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "claude")
        self.OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
        self.OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.OPENROUTER_DEFAULT_MODEL = os.getenv("OPENROUTER_DEFAULT_MODEL", "qwen/qwen3-coder:free")
        self.OPENROUTER_HTTP_TIMEOUT = _get_int_env("OPENROUTER_HTTP_TIMEOUT", 300, minimum=1)
        self.OPENROUTER_MODELS_HTTP_TIMEOUT = _get_int_env("OPENROUTER_MODELS_HTTP_TIMEOUT", 8, minimum=1)
        self.OPENROUTER_MODEL_CACHE_TTL_SECONDS = _get_int_env("OPENROUTER_MODEL_CACHE_TTL_SECONDS", 21600, minimum=0)
        self.LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
        self.LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "")
        self.LOCAL_LLM_DEFAULT_MODEL = os.getenv("LOCAL_LLM_DEFAULT_MODEL", "qwen2.5-coder:7b")
        self.LOCAL_LLM_HTTP_TIMEOUT = _get_int_env("LOCAL_LLM_HTTP_TIMEOUT", 300, minimum=1)
        self.LOCAL_LLM_STARTUP_TIMEOUT = _get_int_env("LOCAL_LLM_STARTUP_TIMEOUT", 600, minimum=1)
        self.LOCAL_LLM_ENABLE_TOOLS = os.getenv("LOCAL_LLM_ENABLE_TOOLS", "0").strip().lower() in {"1", "true", "yes", "on"}
        self.LOCAL_LLM_DISABLE_THINKING = os.getenv("LOCAL_LLM_DISABLE_THINKING", "1").strip().lower() in {"1", "true", "yes", "on"}
        self.LOCAL_LLM_ENABLE_STREAMING = os.getenv("LOCAL_LLM_ENABLE_STREAMING", "0").strip().lower() in {"1", "true", "yes", "on"}
        # Which model answers for each helper role, as provider[:model].
        # Split on the first colon only, because Ollama model names carry a
        # tag after one of their own: local:qwen2.5-coder:7b is the
        # qwen2.5-coder:7b model, not a model called qwen2.5-coder.
        # Bare 'local' means the local provider's configured default.
        self.DELEGATION_ENABLED = os.getenv("DELEGATION_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
        self.DELEGATE_SEARCH = os.getenv("DELEGATE_SEARCH", "local")
        self.DELEGATE_REVIEW = os.getenv("DELEGATE_REVIEW", "local")
        self.DELEGATE_IMPLEMENT = os.getenv("DELEGATE_IMPLEMENT", "local")
        self.CLAUDE_BYPASS_PERMISSIONS = os.getenv("CLAUDE_BYPASS_PERMISSIONS", "0").strip().lower() in {"1", "true", "yes", "on"}
        self.RATE_LIMIT_MAX_REQUESTS = _get_int_env("RATE_LIMIT_MAX_REQUESTS", 20, minimum=1)
        self.RATE_LIMIT_WINDOW_SECONDS = _get_int_env("RATE_LIMIT_WINDOW_SECONDS", 3600, minimum=1)
        self.MAX_PROMPT_LENGTH = _get_int_env("MAX_PROMPT_LENGTH", 12000, minimum=256)
        self.ENABLE_STATUS_HTTP = os.getenv("ENABLE_STATUS_HTTP", "0").strip().lower() in {"1", "true", "yes", "on"}
        self.STATUS_HTTP_HOST = os.getenv("STATUS_HTTP_HOST", "127.0.0.1")
        self.STATUS_HTTP_PORT = _get_int_env("STATUS_HTTP_PORT", 8089, minimum=1)


settings = Settings()
