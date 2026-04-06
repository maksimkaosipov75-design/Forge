import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RateLimitState:
    timestamps: deque[float] = field(default_factory=deque)


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._states: dict[str, RateLimitState] = {}

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        state = self._states.setdefault(key, RateLimitState())
        while state.timestamps and now - state.timestamps[0] >= self.window_seconds:
            state.timestamps.popleft()

        if len(state.timestamps) >= self.max_requests:
            retry_after = int(self.window_seconds - (now - state.timestamps[0]))
            return False, max(1, retry_after)

        state.timestamps.append(now)
        return True, 0
