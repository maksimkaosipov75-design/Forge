import os
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional dependency in minimal environments
    def load_dotenv():
        return False

load_dotenv()


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
        self.RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "20"))
        self.RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "3600"))
        self.MAX_PROMPT_LENGTH = int(os.getenv("MAX_PROMPT_LENGTH", "12000"))
        self.ENABLE_STATUS_HTTP = os.getenv("ENABLE_STATUS_HTTP", "1") not in {"0", "false", "False"}
        self.STATUS_HTTP_HOST = os.getenv("STATUS_HTTP_HOST", "127.0.0.1")
        self.STATUS_HTTP_PORT = int(os.getenv("STATUS_HTTP_PORT", "8089"))


settings = Settings()
