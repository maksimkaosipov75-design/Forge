import json
import os
from pathlib import Path


class CredentialStore:
    def __init__(self, secrets_file: Path | None = None):
        self.secrets_file = secrets_file or (Path.home() / ".forge" / "secrets.json")

    def get_api_key(self, provider: str) -> str:
        payload = self._load_payload()
        providers = payload.get("providers", {})
        if not isinstance(providers, dict):
            return ""
        data = providers.get(provider, {})
        if not isinstance(data, dict):
            return ""
        value = data.get("api_key", "")
        return value if isinstance(value, str) else ""

    def set_api_key(self, provider: str, api_key: str):
        payload = self._load_payload()
        providers = payload.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            payload["providers"] = providers
        providers[provider] = {"api_key": api_key}
        self._save_payload(payload)

    def delete_api_key(self, provider: str):
        payload = self._load_payload()
        providers = payload.get("providers", {})
        if isinstance(providers, dict) and provider in providers:
            del providers[provider]
            self._save_payload(payload)

    def has_api_key(self, provider: str) -> bool:
        return bool(self.get_api_key(provider).strip())

    def configured_providers(self) -> list[str]:
        payload = self._load_payload()
        providers = payload.get("providers", {})
        if not isinstance(providers, dict):
            return []
        return sorted(
            name for name, data in providers.items()
            if isinstance(data, dict) and isinstance(data.get("api_key"), str) and data["api_key"].strip()
        )

    def _load_payload(self) -> dict:
        if not self.secrets_file.exists():
            return {"providers": {}}
        try:
            return json.loads(self.secrets_file.read_text(encoding="utf-8"))
        except Exception:
            return {"providers": {}}

    def _save_payload(self, payload: dict):
        self.secrets_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.secrets_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.secrets_file)
        os.chmod(self.secrets_file, 0o600)
