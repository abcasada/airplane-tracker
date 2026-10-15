from dataclasses import dataclass

FREE_ATTEMPTS = 3
MAX_DELAY_S = 900.0
FORGET_AFTER_S = 3600.0
MAX_KEYS = 10_000


@dataclass
class _Entry:
    failures: int
    last_failure: float
    blocked_until: float


class LoginThrottle:
    """Exponential backoff per key (client IP, username). In memory: one process, single user."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def retry_after(self, keys: list[str], now: float) -> float:
        wait = 0.0
        for k in keys:
            e = self._entries.get(k)
            if e and now - e.last_failure > FORGET_AFTER_S:
                del self._entries[k]
                continue
            if e and e.blocked_until > now:
                wait = max(wait, e.blocked_until - now)
        return wait

    def failure(self, keys: list[str], now: float) -> None:
        if len(self._entries) > MAX_KEYS:
            self._entries.clear()
        for k in keys:
            e = self._entries.get(k) or _Entry(0, now, 0.0)
            e.failures += 1
            e.last_failure = now
            if e.failures > FREE_ATTEMPTS:
                e.blocked_until = now + min(2.0 ** (e.failures - FREE_ATTEMPTS), MAX_DELAY_S)
            self._entries[k] = e

    def success(self, keys: list[str]) -> None:
        for k in keys:
            self._entries.pop(k, None)
