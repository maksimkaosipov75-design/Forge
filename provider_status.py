import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.monotonic()


@dataclass
class FailureReason:
    kind: str = ""
    message: str = ""
    retry_at: str = ""          # human-readable HH:MM string (display only)
    retry_after_ts: float = 0.0 # monotonic timestamp when provider becomes available
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


# Backoff schedule by failure kind (seconds until retry)
_BACKOFF_SECONDS: dict[str, list[int]] = {
    "limit":   [60, 300, 900, 1800],   # 1m → 5m → 15m → 30m
    "auth":    [0],                    # auth won't auto-recover; always 0 = no auto-retry
    "network": [15, 30, 60, 120],      # quick network retries
    "context": [0],                    # context window — no time-based recovery
    "timeout": [30, 60, 120, 300],
    "tool":    [10, 30, 60],
    "unknown": [30, 60, 120],
}


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
    consecutive_failures: int = 0
    # Monotonic timestamp (from time.monotonic()) after which the provider
    # should be considered available again. 0 = no scheduled recovery.
    retry_after_ts: float = 0.0

    def is_available_now(self) -> bool:
        """True if the provider is considered available right now."""
        if self.available:
            return True
        if self.retry_after_ts and _now_ts() >= self.retry_after_ts:
            # Time has passed — optimistically mark available again
            self.available = True
            self.retry_after_ts = 0.0
            self.updated_at = utc_now_iso()
            return True
        return False

    @property
    def degradation_level(self) -> str:
        """'ok' | 'degraded' | 'failing'"""
        if self.consecutive_failures == 0:
            return "ok"
        if self.consecutive_failures <= 2:
            return "degraded"
        return "failing"

    @property
    def retry_in_seconds(self) -> int | None:
        """Seconds remaining until scheduled retry, or None."""
        if not self.retry_after_ts:
            return None
        remaining = self.retry_after_ts - _now_ts()
        return max(0, int(remaining)) if remaining > 0 else None

    def register_failure(self, failure: FailureReason):
        self.available = False
        self.consecutive_failures += 1
        self.last_failure = failure
        self.last_error = failure.message
        self.updated_at = utc_now_iso()

        # Compute backoff duration
        schedule = _BACKOFF_SECONDS.get(failure.kind, _BACKOFF_SECONDS["unknown"])
        idx = min(self.consecutive_failures - 1, len(schedule) - 1)
        backoff = schedule[idx]

        if failure.retry_after_ts:
            # Explicit time extracted from message takes priority
            self.retry_after_ts = failure.retry_after_ts
        elif backoff > 0:
            self.retry_after_ts = _now_ts() + backoff
        else:
            self.retry_after_ts = 0.0  # no auto-recovery (e.g. auth, context)

        if failure.kind == "limit":
            self.last_limit_message = failure.message
            self.last_limit_reset_at = failure.retry_at
        elif failure.kind == "context":
            self.context_status = failure.message

    def register_success(self):
        self.available = True
        self.consecutive_failures = 0
        self.retry_after_ts = 0.0
        self.last_error = ""
        self.last_failure = None
        self.updated_at = utc_now_iso()

    def summary_lines(self) -> list[str]:
        if self.is_available_now():
            state = "🟢 available"
        elif self.degradation_level == "degraded":
            state = "🟡 degraded"
        else:
            state = "🔴 failing"
        lines = [f"<b>{self.provider}</b>: {state}"]
        lines.append(f"Context: <code>{self.context_status}</code>")
        if self.consecutive_failures:
            lines.append(f"Failures: {self.consecutive_failures}  [{self.degradation_level}]")
        if self.last_failure:
            lines.append(f"Last failure: <code>{self.last_failure.short_label}</code>")
            lines.append(f"Reason: {self.last_failure.message}")
            ri = self.retry_in_seconds
            if ri is not None:
                lines.append(f"Retry in: <code>{ri}s</code>")
            elif self.last_failure.retry_at:
                lines.append(f"Retry at: <code>{self.last_failure.retry_at}</code>")
        elif self.last_limit_message:
            lines.append(f"Last limit note: {self.last_limit_message}")
        return lines

    def to_dict(self) -> dict:
        """Serialise for session persistence (only stable fields, not monotonic ts)."""
        return {
            "provider": self.provider,
            "available": self.available,
            "last_error": self.last_error,
            "last_limit_message": self.last_limit_message,
            "last_limit_reset_at": self.last_limit_reset_at,
            "context_status": self.context_status,
            "updated_at": self.updated_at,
            "consecutive_failures": self.consecutive_failures,
            "last_failure_kind": self.last_failure.kind if self.last_failure else "",
            "last_failure_message": self.last_failure.message if self.last_failure else "",
            "last_failure_retry_at": self.last_failure.retry_at if self.last_failure else "",
            # Persist remaining backoff as a wall-clock offset so it survives restart
            "retry_in_seconds": self.retry_in_seconds or 0,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderHealth":
        h = cls(provider=data.get("provider", ""))
        h.available = bool(data.get("available", True))
        h.last_error = data.get("last_error", "")
        h.last_limit_message = data.get("last_limit_message", "")
        h.last_limit_reset_at = data.get("last_limit_reset_at", "")
        h.context_status = data.get("context_status", "unknown")
        h.updated_at = data.get("updated_at", utc_now_iso())
        h.consecutive_failures = int(data.get("consecutive_failures", 0))
        kind = data.get("last_failure_kind", "")
        msg = data.get("last_failure_message", "")
        retry_at = data.get("last_failure_retry_at", "")
        if kind:
            h.last_failure = FailureReason(kind=kind, message=msg, retry_at=retry_at)
        retry_in = int(data.get("retry_in_seconds", 0))
        if retry_in > 0 and not h.available:
            h.retry_after_ts = _now_ts() + retry_in
        return h


def classify_failure_text(text: str) -> FailureReason | None:
    raw = (text or "").strip()
    if not raw:
        return None

    lowered = raw.lower()
    retry_at_str = _extract_retry_at(raw)
    retry_after_ts = _extract_retry_after_ts(raw)

    if any(token in lowered for token in ("you hit your limit", "rate limit", "too many requests", "429", "usage limit", "please wait")):
        return FailureReason(kind="limit", message=_clean_message(raw), retry_at=retry_at_str, retry_after_ts=retry_after_ts, source_text=raw)

    if any(token in lowered for token in ("context window", "context length", "prompt is too long", "too long for context", "max tokens")):
        return FailureReason(kind="context", message=_clean_message(raw), retry_at=retry_at_str, retry_after_ts=retry_after_ts, source_text=raw)

    if any(token in lowered for token in ("unauthorized", "authentication", "api key", "login required", "not logged in", "forbidden")):
        return FailureReason(kind="auth", message=_clean_message(raw), retry_at=retry_at_str, retry_after_ts=retry_after_ts, source_text=raw)

    if any(token in lowered for token in ("connection error", "fetch failed", "network", "reconnecting", "timed out connecting", "temporary failure")):
        return FailureReason(kind="network", message=_clean_message(raw), retry_at=retry_at_str, retry_after_ts=retry_after_ts, source_text=raw)

    if "timeout" in lowered or "timed out" in lowered:
        return FailureReason(kind="timeout", message=_clean_message(raw), retry_at=retry_at_str, retry_after_ts=retry_after_ts, source_text=raw)

    if any(token in lowered for token in ("tool failed", "bash exited", "command failed", "patch failed")):
        return FailureReason(kind="tool", message=_clean_message(raw), retry_at=retry_at_str, retry_after_ts=retry_after_ts, source_text=raw)

    if "error" in lowered or "failed" in lowered or "exception" in lowered:
        return FailureReason(kind="unknown", message=_clean_message(raw), retry_at=retry_at_str, retry_after_ts=retry_after_ts, source_text=raw)

    return None


def _clean_message(text: str, limit: int = 220) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _extract_retry_at(text: str) -> str:
    """Extract human-readable HH:MM retry time from message text."""
    patterns = [
        r"(?:available at|try again at|resets? at|available again at)\s+(\d{1,2}:\d{2})",
        r"(\d{1,2}:\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _extract_retry_after_ts(text: str) -> float:
    """Extract a monotonic retry timestamp from message text when a time is found."""
    # Try to find minutes/seconds patterns: "try again in 5 minutes", "retry after 30s"
    m = re.search(r"(?:in|after|wait)\s+(\d+)\s+(minute|min|second|sec)", text, re.IGNORECASE)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        seconds = amount * 60 if unit.startswith("min") else amount
        return _now_ts() + seconds

    # HH:MM — compute seconds until that time today (or tomorrow)
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        now = datetime.now(timezone.utc)
        target_h, target_m = int(m.group(1)), int(m.group(2))
        target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target += timedelta(days=1)
        delta = (target - now).total_seconds()
        if 0 < delta < 86400:  # sanity check: within 24h
            return _now_ts() + delta

    return 0.0
