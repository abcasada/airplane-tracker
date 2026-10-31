class TokenBucket:
    """Token bucket driven by an injected time value, so tests stay deterministic."""

    def __init__(self, rate_per_s: float, burst: float = 1.0) -> None:
        self.rate = rate_per_s
        self.burst = burst
        self.tokens = burst
        self.updated: float | None = None

    def _refill(self, now: float) -> None:
        if self.updated is not None:
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def wait_time(self, now: float) -> float:
        """Seconds until a token is available (0 if one is available now)."""
        self._refill(now)
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate

    def take(self, now: float) -> bool:
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class CircuitBreaker:
    """Opens after failures; any 4xx/429 opens immediately (adsb.fi IP-blocks repeat 4xx)."""

    def __init__(self, base_s: float = 5.0, max_s: float = 300.0, threshold: int = 3) -> None:
        self.base_s = base_s
        self.max_s = max_s
        self.threshold = threshold
        self.failures = 0
        self.open_until = 0.0
        self.last_error: str | None = None
        self.last_ok_at: float | None = None

    def is_open(self, now: float) -> bool:
        return now < self.open_until

    def success(self, now: float) -> None:
        self.failures = 0
        self.open_until = 0.0
        self.last_ok_at = now

    def failure(self, now: float, error: str, client_error: bool) -> None:
        self.failures += 1
        self.last_error = error
        if client_error:
            # Back off hard: 30 s, 60 s, 120 s … capped.
            delay = 30.0 * 2 ** (self.failures - 1)
        elif self.failures >= self.threshold:
            delay = self.base_s * 2 ** (self.failures - self.threshold)
        else:
            return
        self.open_until = now + min(self.max_s, delay)
