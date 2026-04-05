import os
from dotenv import load_dotenv

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


settings = Settings()
