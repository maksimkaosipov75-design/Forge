import re
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class FailureReason:
    kind: str = ""
    message: str = ""
    retry_at: str = ""
    source_text: str = ""
    detected_at: str = field(default_factory=utc_now_iso)

    @property
    def is_known(self) -> bool:
        return bool(self.kind)

    @property
    def short_label(self) -> str:
        mapping = {
            "limit": "limit reached",
            "auth": "auth required",
            "network": "network issue",
            "context": "context window",
            "timeout": "timeout",
            "tool": "tool failure",
            "unknown": "unknown failure",
        }
        return mapping.get(self.kind, self.kind or "unknown")


@dataclass
class ProviderHealth:
    provider: str
    available: bool = True
    last_error: str = ""
    last_failure: FailureReason | None = None
    last_limit_message: str = ""
    last_limit_reset_at: str = ""
    context_status: str = "unknown"
    updated_at: str = field(default_factory=utc_now_iso)

    def register_failure(self, failure: FailureReason):
        self.available = False
        self.last_failure = failure
        self.last_error = failure.message
        self.updated_at = utc_now_iso()
        if failure.kind == "limit":
            self.last_limit_message = failure.message
            self.last_limit_reset_at = failure.retry_at
        elif failure.kind == "context":
            self.context_status = failure.message

    def register_success(self):
        self.available = True
        self.last_error = ""
        self.last_failure = None
        self.updated_at = utc_now_iso()

    def summary_lines(self) -> list[str]:
        state = "🟢 available" if self.available else "🔴 limited or failing"
        lines = [f"<b>{self.provider}</b>: {state}"]
        lines.append(f"Context: <code>{self.context_status}</code>")
        if self.last_failure:
            lines.append(f"Failure: <code>{self.last_failure.short_label}</code>")
            lines.append(f"Reason: {self.last_failure.message}")
            if self.last_failure.retry_at:
                lines.append(f"Retry at: <code>{self.last_failure.retry_at}</code>")
        elif self.last_limit_message:
            lines.append(f"Last limit note: {self.last_limit_message}")
        return lines


def classify_failure_text(text: str) -> FailureReason | None:
    raw = (text or "").strip()
    if not raw:
        return None

    lowered = raw.lower()
    retry_at = _extract_retry_at(raw)

    if any(token in lowered for token in ("you hit your limit", "rate limit", "too many requests", "429", "usage limit")):
        return FailureReason(kind="limit", message=_clean_message(raw), retry_at=retry_at, source_text=raw)

    if any(token in lowered for token in ("context window", "context length", "prompt is too long", "too long for context", "max tokens")):
        return FailureReason(kind="context", message=_clean_message(raw), retry_at=retry_at, source_text=raw)

    if any(token in lowered for token in ("unauthorized", "authentication", "api key", "login required", "not logged in", "forbidden")):
        return FailureReason(kind="auth", message=_clean_message(raw), retry_at=retry_at, source_text=raw)

    if any(token in lowered for token in ("connection error", "fetch failed", "network", "reconnecting", "timed out connecting", "temporary failure")):
        return FailureReason(kind="network", message=_clean_message(raw), retry_at=retry_at, source_text=raw)

    if "timeout" in lowered or "timed out" in lowered:
        return FailureReason(kind="timeout", message=_clean_message(raw), retry_at=retry_at, source_text=raw)

    if any(token in lowered for token in ("tool failed", "bash exited", "command failed", "patch failed")):
        return FailureReason(kind="tool", message=_clean_message(raw), retry_at=retry_at, source_text=raw)

    if "error" in lowered or "failed" in lowered or "exception" in lowered:
        return FailureReason(kind="unknown", message=_clean_message(raw), retry_at=retry_at, source_text=raw)

    return None


def _clean_message(text: str, limit: int = 220) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _extract_retry_at(text: str) -> str:
    patterns = [
        r"(?:available at|try again at|resets? at|available again at)\s+(\d{1,2}:\d{2})",
        r"(\d{1,2}:\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""
